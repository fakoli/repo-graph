#!/usr/bin/env python3
"""Build an offline, bounded repository map from local paths and imports."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from repo_graph import jev
from repo_graph.jev import typesafe_key
from repo_graph.source import SourceRoot


PAGE_SIZE = 24
READ_LIMIT = 64 * 1024
SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "vendor", "target"}
CODE_EXTENSIONS = {".go", ".py", ".js", ".jsx", ".ts", ".tsx"}
ROLES = ("application", "library", "infrastructure", "tests", "documentation", "examples", "tooling", "other")


def repo_files(root: Path, *, coverage: dict | None = None) -> list[str]:
    command = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    result = subprocess.run(command, cwd=root, capture_output=True, check=False) if shutil.which("git") else None
    if result is not None and result.returncode == 0:
        paths = [Path(os.fsdecode(part)) for part in result.stdout.split(b"\0") if part]
    else:
        paths = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")]
            paths.extend((Path(base) / name).relative_to(root) for name in files)
    valid, failures = set(), []
    with SourceRoot(root) as source:
        for path in paths:
            if not path.parts or any(part.startswith('.') or part in SKIP_DIRS for part in path.parts):
                continue
            try:
                source.info(path.as_posix())
                valid.add(path.as_posix())
            except OSError as error:
                failures.append({'path': path.as_posix(), 'reason': error.strerror})
                continue
        if coverage is not None:
            coverage.update(identity=source.identity, secure_reads=source.secure,
                            failed=len(failures), failures=failures[:50])
    return sorted(valid)


def tree_index(files: list[str]) -> dict[str, dict]:
    tree = {"": {"count": 0, "children": set(), "direct": [], "sample": []}}
    for file in files:
        parent = ""
        for part in PurePosixPath(file).parts[:-1]:
            child = f"{parent}/{part}" if parent else part
            tree.setdefault(child, {"count": 0, "children": set(), "direct": [], "sample": []})
            tree[parent]["children"].add(child)
            tree[parent]["count"] += 1
            if len(tree[parent]["sample"]) < 8:
                tree[parent]["sample"].append(file)
            parent = child
        tree[parent]["count"] += 1
        tree[parent]["direct"].append(file)
        if len(tree[parent]["sample"]) < 8:
            tree[parent]["sample"].append(file)
    for node in tree.values():
        node["children"] = sorted(node["children"])
    return tree


def imports_from(path: str, source: str) -> list[str]:
    ext = posixpath.splitext(path)[1]
    if ext == ".go":
        imports, in_block = [], False
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ("):
                in_block = True
                continue
            if in_block and stripped == ")":
                in_block = False
                continue
            if in_block or stripped.startswith("import "):
                match = re.search(r'"([^"\n]+)"', stripped.split("//", 1)[0])
                if match:
                    imports.append(match.group(1))
        return imports
    if ext == ".py":
        imports = []
        for line in source.splitlines():
            match = re.match(r"\s*(?:from\s+([.\w]+)\s+import|import\s+([\w.]+))", line)
            if match:
                imports.append(match.group(1) or match.group(2))
        return imports
    if ext in {".js", ".jsx", ".ts", ".tsx"}:
        return re.findall(r"""(?:\bfrom\s*|\bimport\s*\(|\brequire\s*\()\s*['"]([^'"]+)['"]""", source)
    return []


def local_target(path: str, imported: str, module: str, tree: dict[str, dict]) -> str | None:
    parent = posixpath.dirname(path)
    ext = posixpath.splitext(path)[1]
    if ext == ".go" and module and imported.startswith(module + "/"):
        target = imported[len(module) + 1:]
    elif ext == ".py":
        if imported.startswith("."):
            dots = len(imported) - len(imported.lstrip("."))
            base = parent
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            target = posixpath.join(base, imported[dots:].replace(".", "/"))
        else:
            target = imported.replace(".", "/")
    elif ext in {".js", ".jsx", ".ts", ".tsx"} and imported.startswith("."):
        target = posixpath.normpath(posixpath.join(parent, imported))
    else:
        return None
    target = target.strip("/")
    if target in tree:
        return target
    parent = posixpath.dirname(target)
    if parent not in tree:
        return None
    candidates = {target, *(target + suffix for suffix in CODE_EXTENSIONS)}
    return parent if candidates.intersection(tree[parent]["direct"]) else None


def extract_dependencies(root: Path, files: list[str], tree: dict[str, dict], cache_path: Path) -> tuple[list[dict], dict]:
    with SourceRoot(root) as source:
        return _extract_dependencies(source, files, tree, cache_path)


def _extract_dependencies(source: SourceRoot, files: list[str], tree: dict[str, dict], cache_path: Path):
    module = ""
    failures = []
    try:
        data, _, _ = source.read('go.mod', 4096, hash_full=False)
        match = re.search(r"(?m)^module\s+(\S+)", data.decode('utf-8', errors='replace'))
        module = match.group(1) if match else ""
    except FileNotFoundError:
        pass
    except OSError as error:
        failures.append({'path': 'go.mod', 'reason': error.strerror})
    identity = {'schema': 2, 'repository': source.identity, 'analyzer': 'imports-v2',
                'read_limit': READ_LIMIT, 'extensions': sorted(CODE_EXTENSIONS)}
    try:
        with SourceRoot(cache_path.parent) as cache_root, cache_root.open(cache_path.name) as stream:
            cached = json.load(stream)
        old = cached['files'] if all(cached.get(k) == v for k, v in identity.items()) else {}
        if not isinstance(old, dict): old = {}
    except (OSError, ValueError, KeyError, AttributeError):
        old = {}
    current, edges = {}, Counter()
    scanned = reused = truncated = 0
    # ponytail: one metadata pass and 512-file batches; use SQLite only if a multi-million-file repo needs it.
    for start in range(0, len(files), 512):
        for file in files[start:start + 512]:
            if posixpath.splitext(file)[1] not in CODE_EXTENSIONS:
                continue
            try:
                data, digest, stat = source.read(file, READ_LIMIT)
                cached = old.get(file)
                if (isinstance(cached, dict) and cached.get('digest') == digest
                        and isinstance(cached.get('imports'), list) and all(isinstance(i, str) for i in cached['imports'])):
                    imports = cached['imports']
                    reused += 1
                else:
                    imports = imports_from(file, data.decode('utf-8', errors='replace'))
                    scanned += 1
                truncated += stat.st_size > READ_LIMIT
            except OSError as error:
                failures.append({'path': file, 'reason': error.strerror})
                continue
            current[file] = {'digest': digest, 'imports': imports}
            source_dir = posixpath.dirname(file)
            for imported in imports:
                target_dir = local_target(file, imported, module, tree)
                if target_dir is not None and target_dir != source_dir:
                    edges[source_dir, target_dir] += 1
    with SourceRoot(cache_path.parent) as cache_root:
        if cache_root.secure:
            cache_root.write_json(cache_path.name, dict(identity, files=current))
    return ([{"source": a, "target": b, "count": n, "relation": "imports"}
             for (a, b), n in sorted(edges.items())],
            {"scanned": scanned, "reused": reused, "truncated": truncated, "code_files": len(current),
             'identity': identity, 'failed': len(failures), 'failures': failures[:50], 'secure_reads': source.secure})


def scope_edges(dependencies: list[dict]) -> dict[str, list[dict]]:
    scopes: dict[str, Counter] = {}
    for edge in dependencies:
        a = edge["source"].split("/") if edge["source"] else []
        b = edge["target"].split("/") if edge["target"] else []
        common = 0
        while common < min(len(a), len(b)) and a[common] == b[common]:
            common += 1
        scope = "/".join(a[:common])
        source = "/".join(a[:common + 1]) if len(a) > common else f"scope:{scope}"
        target = "/".join(b[:common + 1]) if len(b) > common else f"scope:{scope}"
        if source != target:
            scopes.setdefault(scope, Counter())[source, target] += edge["count"]
    return {scope: [{"source": a, "target": b, "count": n, "relation": "imports"}
                    for (a, b), n in sorted(links.items(), key=lambda item: (-item[1], item[0]))]
            for scope, links in scopes.items()}


def mermaid(tree: dict[str, dict], edges: dict[str, list[dict]]) -> str:
    children = (tree[""]["children"] + tree[""]["direct"])[:PAGE_SIZE - 1]
    ids = {path: f"n{index + 1}" for index, path in enumerate(children)}
    lines = ["flowchart LR", f'    n0["Repository ({tree[""]["count"]} files)"]']
    for path in children:
        label = re.sub(r"[^\w ./()-]", " ", path)[:60].replace('"', " ")
        count = tree[path]["count"] if path in tree else 1
        lines.append(f'    {ids[path]}["{label} ({count} files)"]')
        lines.append(f'    n0 -.-> {ids[path]}')
    for edge in edges.get("", [])[:40 - len(children)]:
        source = "n0" if edge["source"] == "scope:" else ids.get(edge["source"])
        target = "n0" if edge["target"] == "scope:" else ids.get(edge["target"])
        if source and target:
            lines.append(f'    {source} -->|{edge["count"]} imports| {target}')
    return "\n".join(lines) + "\n"


def jev_roles(names: list[str], key: str) -> dict[str, str]:
    if not names:
        return {}
    questions = {f"c{i}": {"type": "choice",
                         "instructions": f"Classify state.directories[{i}] by its likely software role from its name only.",
                         "criteria": {role: role for role in ROLES}}
                 for i in range(len(names))}
    payload = jev.evaluate(jev.body({"directories": names}, questions), key, timeout=3)
    answers = payload['answers']
    roles = {}
    for i, name in enumerate(names):
        answer = answers.get(f"c{i}", {})
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence", 0) if isinstance(answer, dict) else 0
        if choice in ROLES and type(confidence) in (float, int) and .6 <= confidence <= 1:
            roles[name] = choice
    return roles


def system_view(tree: dict[str, dict], dependencies: list[dict], roles: dict[str, str]) -> dict:
    """A disjoint, bounded partition of repository files into inspectable components."""
    groups = []
    categories = {
        "docs": ("Documentation", "documentation"), "website": ("Documentation", "documentation"),
        "examples": ("Examples", "examples"), "tests": ("Tests", "tests"), "test": ("Tests", "tests"),
        "tools": ("Developer tools", "tooling"), "scripts": ("Developer tools", "tooling"),
        "generate": ("Developer tools", "tooling"), "generator": ("Developer tools", "tooling"),
        "acctest": ("Tests", "tests"),
        "infrastructure": ("Infrastructure", "infrastructure"), "deploy": ("Infrastructure", "infrastructure"),
    }
    layers = {"runtime": 1, "application": 1, "library": 1, "tooling": 2, "infrastructure": 2, "tests": 2,
              "documentation": 3, "examples": 3, "other": 2}
    def add(name: str, paths: list[str], count: int, role: str, summary: str) -> None:
        if count:
            existing = next((group for group in groups if group["name"] == name and group["role"] == role), None)
            if existing:
                existing["paths"].extend(paths)
                existing["count"] += count
                return
            groups.append({"name": name, "paths": paths, "count": count, "role": role,
                           "layer": layers.get(role, 2), "summary": summary})
    add("Root files", [""], len(tree[""]["direct"]), "entry", "Top-level source and configuration")
    for path in tree[""]["children"]:
        branch = tree[path]
        if path.lower() in {"src", "app", "apps", "packages", "internal", "pkg", "lib", "services", "server", "client"} and branch["children"]:
            priority = {"provider": 0, "api": 0, "core": 0, "service": 1, "services": 1,
                        "framework": 2, "conns": 3, "server": 3, "client": 3, "storage": 3}
            children = []
            for child in branch["children"]:
                category = categories.get(posixpath.basename(child).lower())
                if category:
                    add(category[0], [child], tree[child]["count"], category[1], f"{category[0]} source directories")
                else:
                    children.append(child)
            children.sort(key=lambda child: (priority.get(posixpath.basename(child).lower(), 4), -tree[child]["count"], child))
            for child in children[:4]:
                add(child, [child], tree[child]["count"], "runtime", f"Code and assets under {child}")
            remaining = children[4:]
            add(f"Shared {path} components", remaining + ([path] if branch["direct"] else []),
                sum(tree[child]["count"] for child in remaining) + len(branch["direct"]),
                "runtime", "Additional packages in this source area")
        else:
            name, role = categories.get(path.lower(), (path, roles.get(path, "other")))
            add(name, [path], branch["count"], role, f"{name} source directories" if path.lower() in categories else f"Source area: {path}")
    if len(groups) > 12:
        ranked = sorted(groups, key=lambda group: (group["role"] not in {"entry", "runtime", "application"}, -group["count"], group["name"]))
        kept, remainder = ranked[:11], ranked[11:]
        groups = kept + [{"name": "Other components", "paths": [path for group in remainder for path in group["paths"]],
                         "count": sum(group["count"] for group in remainder), "role": "other", "layer": 2,
                         "summary": "Additional source areas; inspect to explore"}]
    groups.sort(key=lambda group: (0 if group["role"] == "entry" else group["layer"], group["name"]))
    explicit = {}
    for index, group in enumerate(groups):
        group.update(id=f"system:{index}", kind="system", layer=0 if group["role"] == "entry" else group["layer"])
        group["files"] = [file for path in group["paths"] for file in tree[path]["direct"][:2]][:8]
        for path in group["paths"]:
            explicit[path] = group["id"]
    owners = {}
    for path in sorted(tree, key=lambda path: (path.count("/") + bool(path), path)):
        owners[path] = explicit.get(path, owners.get(posixpath.dirname(path)))
    edges = Counter()
    for edge in dependencies:
        a, b = owners.get(edge["source"]), owners.get(edge["target"])
        if a and b and a != b:
            edges[a, b] += edge["count"]
    return {"nodes": groups, "edges": [{"source": a, "target": b, "count": n, "relation": "imports"}
                                      for (a, b), n in sorted(edges.items(), key=lambda item: (-item[1], item[0]))]}


def offline_index_status(output: Path) -> dict:
    """Export captured readiness and counts, without facts or raw diagnostics."""
    from repo_graph.search import index_status
    captured = index_status(output)
    result = {key: captured[key] for key in ('status', 'error_kind') if key in captured}
    result['capture_kind'] = 'offline'
    fields = ('state', 'artifact_ready', 'query_available', 'freshness', 'freshness_basis',
              'identities', 'receipt_knowledge', 'attempt_attribution', 'model', 'backend_available',
              'keyword_query_available', 'semantic_artifact_ready', 'semantic_query_available', 'semantic_state')
    for name in ('structural', 'semantic_index', 'function_evidence'):
        component = captured[name]
        row = {key: component[key] for key in fields if key in component}
        receipt = component.get('receipt') or {}
        row['receipt'] = {}
        for key, allowed in (('versions', ('schema', 'rules', 'grammars')),
                             ('revision_dirty', ('revision', 'dirty', 'knowledge', 'content_identity'))):
            if key in receipt:
                row['receipt'][key] = {field: receipt[key][field] for field in allowed if field in receipt[key]}
        if 'coverage' in receipt:
            allowed = ('files_total', 'files_supported', 'files_unsupported', 'status_counts', 'file_status',
                       'by_language', 'language_overflow', 'sites_by_role_certainty', 'parser_error_count',
                       'parser_error_samples_truncated', 'inventory_scope', 'discovery_skipped_files',
                       'discovery_skip_knowledge')
            row['receipt']['coverage'] = {key: receipt['coverage'][key] for key in allowed if key in receipt['coverage']}
            if 'by_language' in row['receipt']['coverage']:
                row['receipt']['coverage']['by_language'] = {language:
                    {field: data[field] for field in ('files_total', 'file_status') if field in data}
                    for language, data in row['receipt']['coverage']['by_language'].items()}
        attempt = component.get('last_attempt')
        row['last_attempt'] = ({key: attempt[key] for key in ('status', 'error_kind') if key in attempt}
                               if attempt else None)
        if component.get('catalog_receipt'):
            row['catalog_receipt'] = {key: component['catalog_receipt'][key]
                for key in ('documents', 'truncated', 'failed', 'scanned', 'reused', 'deleted')
                if key in component['catalog_receipt']}
        result[name] = row
    if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > 65536:
        raise ValueError('Offline index status exceeds 65536 bytes')
    return result


def write_page(path: Path, data: dict) -> None:
    template = Path(__file__).resolve().parent / "assets" / "diagram.html"
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = template.read_text(encoding="utf-8").replace("__VIEW_HELPERS__", template.with_name("views.js").read_text(encoding="utf-8"))
    path.write_text(html.replace("__REPO_GRAPH_DATA__", payload), encoding="utf-8")


def source_root(value: str, cache: Path, refresh: bool = False) -> Path:
    if not value.startswith("https://"):
        return Path(value).expanduser().resolve()
    url = urlsplit(value)
    if url.username or url.password or not url.hostname or url.query or url.fragment:
        raise ValueError("Use a public HTTPS repository URL without credentials or query parameters.")
    name = Path(url.path.rstrip("/")).name.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid repository URL.")
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    dest = cache / "sources" / f"{name}-{digest}"
    if dest.exists() and not (dest / ".git").is_dir():
        raise RuntimeError("Cached clone is incomplete; remove that cache entry and retry.")
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--single-branch", value, str(dest)],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError("Repository clone failed. Check the URL and git access.")
    elif refresh:
        result = subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("Could not refresh cached clone; its checkout may have changed.")
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default=".", help="local path or public HTTPS git URL")
    parser.add_argument("--output", type=Path, help="output directory (default: user cache)")
    parser.add_argument("--refresh", action="store_true", help="update a cached HTTPS clone before scanning")
    parser.add_argument("--jev", action="store_true", help="optionally classify top-level directories using TypeSafe System One")
    args = parser.parse_args(argv)
    cache = Path.home() / ".cache" / "repo-graph"
    root = source_root(args.repo, cache, args.refresh)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    output = (args.output or cache / f"{root.name}-{digest}").expanduser().resolve()
    if output == root or root in output.parents:
        parser.error("output directory must be outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    inventory = {}
    files = repo_files(root, coverage=inventory)
    if not files:
        raise RuntimeError("No readable repository files found.")
    tree = tree_index(files)
    top_names = tree[""]["children"][:16]
    role_cache = output / "jev-roles.json"
    role_key = hashlib.sha256(json.dumps([jev.MODEL,ROLES,top_names]).encode()).hexdigest()
    roles, jev_status, result = {}, "off", {}
    started, worker = time.monotonic(), None
    if args.jev:
        key = typesafe_key()
        if not key:
            jev_status = "unavailable: TYPESAFE_API_KEY was not found"
        else:
            try:
                cached = json.loads(role_cache.read_text(encoding="utf-8"))
                if cached.get("key") == role_key:
                    roles, jev_status = cached["roles"], "cached"
            except (OSError, ValueError, KeyError):
                pass
            if jev_status != "cached":
                jev_status = "pending"
                def classify() -> None:
                    try:
                        result["roles"] = jev_roles(top_names, key)
                    except (OSError, RuntimeError, ValueError, KeyError) as error:
                        result["error"] = type(error).__name__
                        result["roles"] = {}
                worker = threading.Thread(target=classify, daemon=True)
                worker.start()
    dependencies, scan = extract_dependencies(root, files, tree, output / "scan-cache.json")
    scan['inventory'] = inventory
    if worker:
        worker.join(timeout=max(0, 3.2 - (time.monotonic() - started)))
        if not worker.is_alive():
            roles = result.get("roles", {})
            jev_status = f"unavailable: {result['error']}" if "error" in result else "used" if roles else "no confident roles"
            if "error" not in result:
                role_cache.write_text(json.dumps({"key": role_key, "roles": roles}), encoding="utf-8")
        else:
            jev_status = "timed out"
    from repo_graph.search import catalog
    search = {'documents': 0, 'scanned': 0, 'reused': 0, 'secure_reads': False,
              'status': 'unavailable', 'reason': 'Secure source reads unavailable; metadata-only offline map'}
    if scan['secure_reads']:
        try:
            search = catalog(root, files, output)
        except OSError:
            search['reason'] = 'Secure index access unavailable; offline map preserved'
    edges = scope_edges(dependencies)
    display_name = Path(urlsplit(args.repo).path.rstrip("/")).name.removesuffix(".git") if args.repo.startswith("https://") else root.name
    graph = {"schema": 1, "name": display_name, "file_count": len(files), "files": files,
             "tree": tree, "dependencies": dependencies, "scope_edges": edges,
             "scan": scan, "jev": jev_status, "roles": roles,
             "system": system_view(tree, dependencies, roles), "search": search,
             "index_status": offline_index_status(output)}
    graph_path = output / "graph.json"
    graph_path.write_text(json.dumps(graph, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    view = {key: graph[key] for key in ("name", "file_count", "tree", "scope_edges", "scan", "jev", "roles", "system", "search", "index_status")}
    for name in ("architecture.html", "graph.html"):
        write_page(output / name, view)
    diagram = mermaid(tree, edges)
    (output / "architecture.mmd").write_text(diagram, encoding="utf-8")
    (output / "architecture.md").write_text("# Repository architecture\n\n```mermaid\n" + diagram + "```\n", encoding="utf-8")
    print(f"Search: {search['documents']} documents, {search['scanned']} indexed, {search['reused']} reused")
    print(f"{len(files)} files, {len(tree) - 1} directories, {len(dependencies)} local import links; "
          f"{scan['scanned']} scanned, {scan['reused']} cached, {scan['truncated']} truncated; Jev: {jev_status}")
    for label, name in (("Diagram", "architecture.html"), ("Graph", "graph.html"),
                        ("Mermaid", "architecture.mmd"), ("JSON", "graph.json")):
        print(f"{label}: {output / name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"repo-graph: {error}", file=sys.stderr)
        raise SystemExit(1) from error
