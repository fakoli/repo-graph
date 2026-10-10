"""Bounded, source-only review packets for independent human or Codex review.

This deliberately does not invoke tests, import repository code, or schedule workers.
"""
from __future__ import annotations

import ast
from collections import Counter
import errno
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import time
import uuid
try:
    import fcntl
except ImportError:
    fcntl = None

from .builder import repo_files
from .source import SourceRoot

SCHEMA = "repo-graph-review-v3"
MAX_FILES = 2_000
MAX_FILE_BYTES = 256 * 1024
MAX_PACKET_BYTES = 768 * 1024
MAX_PACKETS = 400
MAX_RESULT_BYTES = 128 * 1024
MAX_FINDINGS = 100
MAX_TEXT = 8_192
MAX_SOURCE_BYTES = 32 * 1024 * 1024
PACKET_BASIS_KEYS = ("packet_id", "test", "test_members", "sources", "source_text", "gaps", "anchors", "bytes", "actual_collected_parameter_count", "scope_digest", "review_scope", "dependencies_complete")
PACKET_CONSTRUCTION = "integration-members-v1"
CLOSURE_GAP = "closure_omissions_unknown;runtime_and_unvisited_edges_are_unqualified"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _parse_json(raw, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {label}")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant in {label}")))
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"invalid {label} JSON") from error
    if type(value) is not dict:
        raise ValueError(f"{label} JSON object required")
    return value


def _fail(message):
    raise ValueError(message)


def _relative(path: str) -> str:
    SourceRoot.parts(path)
    return PurePosixPath(path).as_posix()


def _limits(value):
    defaults = {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES,
                "packet_bytes": MAX_PACKET_BYTES, "packets": MAX_PACKETS,
                "result_bytes": MAX_RESULT_BYTES, "source_bytes": MAX_SOURCE_BYTES}
    if value is None:
        return defaults
    if type(value) is not dict or set(value) - set(defaults):
        _fail("limits may only reduce built-in review ceilings")
    for key, number in value.items():
        if type(number) is not int or number < 1 or number > defaults[key]:
            _fail(f"limit {key} must be a positive value at or below the built-in ceiling")
        defaults[key] = number
    return defaults


def _read_complete(source, path, ceiling, *, remaining=None):
    try:
        info = source.info(path)
    except OSError:
        return None, {"path": path, "reason": "source_unavailable", "read_bytes": 0}
    if info.st_size > ceiling:
        return None, {"path": path, "reason": "oversized", "bytes": info.st_size}
    if remaining is not None and info.st_size > remaining:
        return None, {"path": path, "reason": "source_read_budget", "bytes": info.st_size}
    measurements = {}
    try:
        raw, digest, after = source.read(path, info.st_size, max_bytes=min(info.st_size, remaining) if remaining is not None else info.st_size, measurements=measurements)
    except OSError as error:
        if error.errno == errno.EFBIG:
            return None, {"path": path, "reason": "source_read_budget", "bytes": info.st_size, "read_bytes": measurements.get("stream_bytes", 0)}
        return None, {"path": path, "reason": "source_unavailable", "bytes": info.st_size, "read_bytes": measurements.get("stream_bytes", 0)}
    if len(raw) != after.st_size:
        _fail(f"partial source read refused: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, {"path": path, "reason": "non_utf8_source", "bytes": len(raw), "read_bytes": measurements.get("stream_bytes", len(raw))}
    return {"path": path, "sha256": digest, "bytes": len(raw), "text": text}, None


def _unit_fragments(path, item):
    """Whole top-level test functions only; classes need fixture semantics we do not split."""
    # ponytail: split top-level functions; add classes after fixture semantics are qualified.
    tree = ast.parse(item["text"], filename=path)
    lines = item["text"].splitlines(keepends=True)
    units = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
            continue
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        while start > 1 and (not lines[start - 2].strip() or lines[start - 2].lstrip().startswith("#")):
            start -= 1
        end = node.end_lineno
        raw = "".join(lines[start - 1:end]).encode()
        units.append({"start_line": start, "end_line": end, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "text": raw.decode()})
    return units


def _is_test(path):
    name = PurePosixPath(path).name
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _ancestors(path, files):
    parent = PurePosixPath(path).parent
    result = []
    while True:
        candidate = "conftest.py" if str(parent) in ("", ".") else f"{parent.as_posix()}/conftest.py"
        if candidate in files:
            result.append(candidate)
        if str(parent) in ("", "."):
            break
        parent = parent.parent
    return list(reversed(result))


def _targets(path, tree, text):
    """AST imports are candidate evidence, never a dependency-complete claim."""
    try:
        parsed = ast.parse(text, filename=path)
    except SyntaxError as error:
        return [], [f"syntax_error:{error.lineno}"]
    imports, unresolved, resolved_bases = [], [], set()
    parent = PurePosixPath(path).parent
    for node in ast.walk(parsed):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        names = ([name.name for name in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] + [(f"{node.module}.{name.name}" if node.module else name.name) for name in node.names])
        for name in names:
            if isinstance(node, ast.ImportFrom) and node.level:
                base = parent
                for _ in range(max(0, node.level - 1)):
                    base = base.parent
                stem = base / name.replace(".", "/")
            else:
                stem = PurePosixPath(name.replace(".", "/"))
            choices = [f"{stem.as_posix()}.py", f"{stem.as_posix()}/__init__.py"]
            found = next((choice for choice in choices if choice in tree), None)
            if found:
                imports.append(found)
                resolved_bases.add(name)
            elif name:
                base = name.rsplit(".", 1)[0]
                if base not in resolved_bases:
                    unresolved.append(name)
    return sorted(set(imports)), sorted(set(unresolved))


def _package_inits(path, files):
    result, parent = [], PurePosixPath(path).parent
    while str(parent) not in ("", "."):
        candidate = f"{parent.as_posix()}/__init__.py"
        if candidate in files: result.append(candidate)
        parent = parent.parent
    return result


def _local_closure(source, files, initial, limits, used, packet, seen=()):
    """Finite AST candidates only; runtime/plugin edges stay explicit gaps."""
    pending, seen, found, gaps = list(initial), set(seen), [], []
    def fits(items, details):
        sources = [*packet["sources"], *items]
        payload = _packet_payload(packet["test"], sources, [*packet["gaps"], *details], packet["anchors"], sum(row.get("fragment", {}).get("bytes", row["bytes"]) for row in sources), "0" * 64, test_members=packet.get("test_members"))
        return len(_json(payload).encode()) <= limits["packet_bytes"]
    def stop(reason):
        # The shorter summary replaces space reserved in every primary packet.
        packet["gaps"][packet["gaps"].index(CLOSURE_GAP)] = f"closure_stopped:{reason};known_omissions>=1;pending_unknown"
    def add_gap(detail):
        if fits(found, [*gaps, detail]):
            gaps.append(detail)
            return True
        stop("gap_budget")
        return False
    while pending:
        path = pending.pop(0)
        if path in seen: continue
        seen.add(path)
        if _sensitive(path):
            if not add_gap(f"sensitive_dependency_excluded:{path}"): break
            continue
        try: info = source.info(path)
        except OSError:
            if not add_gap(f"missing_dependency:{path}"): break
            continue
        if used[0] + info.st_size > limits["source_bytes"]:
            if not add_gap(f"source_read_budget_dependency:{path}"): break
            continue
        item, problem = _read_complete(source, path, limits["file_bytes"], remaining=limits["source_bytes"] - used[0])
        if problem:
            used[0] += problem.get("read_bytes", 0)
            if not add_gap(f"blocked_dependency:{path}:{problem['reason']}"): break
            continue
        used[0] += item["bytes"]
        imports, unresolved = _targets(path, set(files), item["text"])
        derived = [f"unresolved_import:{path}:{name}" for name in unresolved] + [f"dynamic_dependency_edges_unresolved:{path}"]
        if not fits([*found, item], [*gaps, *derived]):
            stop("packet_budget")
            detail = f"packet_budget_dependency:{path}"
            if fits(found, [*gaps, detail]):
                gaps.append(detail)
            break
        found.append(item)
        pending.extend(_package_inits(path, set(files)) + imports)
        gaps.extend(derived)
    return found, gaps


def _anchors(path, text):
    parsed = ast.parse(text)
    tests, positions, gaps = [], [], []
    for node in ast.walk(parsed):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            tests.append({"path": path, "name": node.name, "line": node.lineno, "end_line": node.end_lineno})
        if isinstance(node, ast.Assert):
            positions.append((node.lineno, node.col_offset))
        if isinstance(node, ast.Call):
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if attr.startswith("assert") or attr.startswith("assert_called"):
                positions.append((node.lineno, node.col_offset))
            elif attr.startswith("assert_"):
                gaps.append(f"dynamic_custom_assertion:{path}:{node.lineno}")
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expression = item.context_expr
                if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute) and expression.func.attr == "raises":
                    positions.append((node.lineno, node.col_offset))
    counts = Counter(line for line, _ in positions)
    assertions = [f"{path}:{line}" if counts[line] == 1 else f"{path}:{line}:{column + 1}" for line, column in sorted(set(positions))]
    return sorted(tests, key=lambda item: item["line"]), assertions, sorted(set(gaps))


def _sensitive(path):
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    return (any(part.lower() in {"secret", "secrets", "credential", "credentials"} for part in parts)
            or name == ".env" or name.startswith(".env.") or "credential" in name or "secret" in name)


def _cache(output):
    path = (Path(output).expanduser() if output else Path.home() / ".cache/repo-graph/reviews").resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _campaign_path(campaign):
    path = Path(campaign).expanduser().resolve()
    if not path.is_dir():
        _fail("campaign must be an existing campaign directory")
    return path


@contextmanager
def _locked(path):
    with SourceRoot(path) as boundary:
        if fcntl is None:
            raise RuntimeError("campaign locking requires descriptor flock support")
        try:
            fcntl.flock(boundary.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("campaign coordinator is locked") from error
        try:
            yield boundary
        finally:
            fcntl.flock(boundary.fd, fcntl.LOCK_UN)


def _load(boundary):
    raw, _, _ = boundary.read("campaign.json", 2 * 1024 * 1024, max_bytes=2 * 1024 * 1024)
    try:
        value = _parse_json(raw, "campaign manifest")
    except ValueError as error:
        raise ValueError("invalid campaign manifest") from error
    if value.get("schema") != SCHEMA:
        _fail("unsupported campaign schema")
    return value


def _packet_basis(packet):
    return {key: packet[key] for key in PACKET_BASIS_KEYS}


def _load_packet(boundary, reference):
    raw, _, _ = boundary.read(reference["path"], 2 * 1024 * 1024, max_bytes=2 * 1024 * 1024)
    packet = _parse_json(raw, "packet")
    if set(packet) != {"schema", "payload_sha256", *PACKET_BASIS_KEYS} or packet.get("schema") != SCHEMA or not isinstance(packet.get("payload_sha256"), str):
        _fail("invalid immutable packet shape")
    if (type(packet["sources"]) is not list or type(packet["source_text"]) is not list
            or type(packet["anchors"]) is not dict or type(packet["test_members"]) is not list
            or not packet["test_members"] or packet["test"] not in packet["test_members"]
            or any(type(path) is not str for path in packet["test_members"])
            or len(packet["test_members"]) != len(set(packet["test_members"]))):
        _fail("invalid immutable packet content")
    if packet.get("packet_id") != reference["packet_id"]:
        _fail("packet reference identity mismatch")
    if reference.get("test_members") != packet["test_members"]:
        _fail("packet reference test member mismatch")
    if packet.get("payload_sha256") != reference.get("payload_sha256") or packet["payload_sha256"] != _digest(_packet_basis(packet)):
        _fail("packet payload identity mismatch")
    if not all(type(item) is dict and type(item.get("path")) is str and type(item.get("text")) is str for item in packet["source_text"]):
        _fail("invalid packet source text")
    texts = {item.get("path"): item.get("text") for item in packet["source_text"]}
    if set(texts) != {item["path"] for item in packet.get("sources", [])}:
        _fail("packet source text inventory mismatch")
    for item in packet["sources"]:
        if type(item) is not dict or not all(key in item for key in ("path", "sha256", "bytes", "start_line", "end_line")):
            _fail("invalid packet source metadata")
        text = texts[item["path"]]
        fragment = item.get("fragment")
        expected_bytes = fragment["bytes"] if fragment else item["bytes"]
        expected_hash = fragment["sha256"] if fragment else item["sha256"]
        if type(text) is not str or len(text.encode("utf-8")) != expected_bytes or hashlib.sha256(text.encode()).hexdigest() != expected_hash:
            _fail("packet source text identity mismatch")
    source_paths = {item["path"] for item in packet["sources"]}
    if not set(packet["test_members"]).issubset(source_paths):
        _fail("packet test member source inventory mismatch")
    if len(packet["test_members"]) > 1:
        for item in packet["sources"]:
            if item["path"] in packet["test_members"] and (item.get("fragment") or item["start_line"] != 1 or item["end_line"] != texts[item["path"]].count("\n") + 1):
                _fail("group test members must be complete files")
    for key in ("state", "attempts", "result", "decision", "stale_reason"):
        packet[key] = reference.get(key)
    return packet


def _save_packet(boundary, reference, packet):
    for key in ("state", "attempts", "result", "decision", "stale_reason"):
        reference[key] = packet.get(key)


def _result_artifact(boundary, state, packet):
    try:
        raw, _, _ = boundary.read(packet["result"]["path"], state["limits"]["result_bytes"], max_bytes=state["limits"]["result_bytes"])
        stored = _parse_json(raw, "recorded result")
        if _digest(stored) != packet["result"]["sha256"]:
            _fail("recorded result digest mismatch")
        _validate_result(packet, stored, state["limits"]["result_bytes"])
        return stored
    except (OSError, ValueError) as error:
        raise ValueError("recorded result is unavailable or invalid") from error


def _save(boundary, campaign):
    if len(_json(campaign).encode()) > 2 * 1024 * 1024:
        _fail("campaign manifest exceeds bounded publication ceiling")
    boundary.write_json("campaign.json", campaign)


def _source_ok(campaign, packet):
    root = Path(campaign["repository"]["path"])
    try:
        with SourceRoot(root) as source:
            if source.identity != campaign["repository"]["binding"]:
                return False, "repository_binding_changed"
            for item in packet["sources"]:
                current, blocked = _read_complete(source, item["path"], campaign["limits"]["file_bytes"])
                if blocked:
                    return False, f"source_unavailable:{item['path']}" if blocked["reason"] == "source_unavailable" else f"source_changed:{item['path']}"
                if current["sha256"] != item["sha256"]:
                    return False, f"source_changed:{item['path']}"
    except OSError as error:
        return False, f"source_unavailable:{error.strerror or error.__class__.__name__}"
    return True, None


def result_schema(maximum=MAX_RESULT_BYTES):
    return {"schema": SCHEMA, "worker_result": {"required": ["packet_id", "attempt_id", "worker_id", "reviewed_ranges", "findings", "assertion_map", "outcome"],
        "outcome": ["completed", "needs_source", "uncertain", "cancelled"],
        "reviewed_ranges": {"required": ["path", "start_line", "end_line", "sha256"]},
        "assertion_map": {"required": ["original", "disposition", "evidence"], "original": "path:line", "dispositions": ["preserved", "combined", "duplicate_with_evidence", "unresolved"]},
        "finding": {"required": ["summary", "citations"], "citation": {"required": ["path", "start_line", "end_line", "sha256"]}},
        "limits": {"bytes": maximum, "findings": MAX_FINDINGS, "text": MAX_TEXT}},
        "review_scope": "materialized_source_only", "dependencies_complete": False,
        "independent_decision": {"required": ["kind", "packet_id", "attempt_id", "result_sha256", "reviewer_id", "disposition", "provenance", "rationale"],
          "kind": "independent_decision", "disposition": ["accepted", "rejected", "needs_source"]}}


def _packet_payload(test, sources, gaps, anchors, total, scope_digest, packet_id="0" * 24, *, test_members=None):
    records = [{k: value for k, value in item.items() if k != "text"} | {"start_line": item.get("fragment", {}).get("start_line", 1), "end_line": item.get("fragment", {}).get("end_line", item["text"].count("\n") + 1)} for item in sources]
    packet = {"schema": SCHEMA, "packet_id": packet_id, "test": test,
              "test_members": list(test_members or [test]), "sources": records, "review_scope": "materialized_source_only", "dependencies_complete": False,
              "source_text": [{"path": item["path"], "text": item["text"]} for item in sources], "gaps": sorted(set(gaps)), "anchors": anchors,
              "bytes": total, "actual_collected_parameter_count": "unknown", "scope_digest": scope_digest}
    packet["payload_sha256"] = _digest(_packet_basis(packet))
    return packet


def _group_spec(source, files, members, explicit, limits, used):
    """Build one explicit whole-file test group; never degrade it to a subset."""
    roots, tests, assertions, gaps, dependencies = [], [], [], [], []
    file_set = set(files)
    for member in members:
        if _sensitive(member):
            return None, member, "sensitive_source_excluded"
        try:
            info = source.info(member)
        except OSError:
            return None, member, "source_unavailable"
        if used[0] + info.st_size > limits["source_bytes"]:
            return None, member, "source_read_budget"
        item, problem = _read_complete(source, member, limits["file_bytes"], remaining=limits["source_bytes"] - used[0])
        if problem:
            used[0] += problem.get("read_bytes", 0)
            return None, member, problem["reason"]
        used[0] += item["bytes"]
        try:
            member_tests, member_assertions, anchor_gaps = _anchors(member, item["text"])
        except SyntaxError as error:
            return None, member, f"syntax_error:{error.lineno}"
        imports, unresolved = _targets(member, file_set, item["text"])
        roots.append(item)
        tests.extend(member_tests); assertions.extend(member_assertions)
        gaps.extend([*anchor_gaps, *(f"unresolved_import:{member}:{name}" for name in unresolved)])
        dependencies.extend(_ancestors(member, file_set) + _package_inits(member, file_set) + imports)
    anchors = {"tests": sorted(tests, key=lambda row: (row["path"], row["line"], row["name"])),
               "assertions": sorted(set(assertions))}
    gaps = ["dynamic fixtures, plugins, parametrization and callers may be unresolved", CLOSURE_GAP, *gaps]
    total = sum(item["bytes"] for item in roots)
    primary = {"test": members[0], "test_members": members, "sources": roots, "gaps": gaps, "anchors": anchors}
    if len(_json(_packet_payload(members[0], roots, gaps, anchors, total, "0" * 64, test_members=members)).encode()) > limits["packet_bytes"]:
        return None, members[0], "group_serialized_budget"
    closure, closure_gaps = _local_closure(source, files, sorted(set(dependencies + list(explicit)) - set(members)), limits, used, primary, set(members))
    sources = [*roots, *closure]
    total += sum(item["bytes"] for item in closure)
    gaps = sorted(set([*gaps, *closure_gaps]))
    if len(_json(_packet_payload(members[0], sources, gaps, anchors, total, "0" * 64, test_members=members)).encode()) > limits["packet_bytes"]:
        return None, members[0], "group_serialized_budget"
    return {"test": members[0], "test_members": members, "sources": sources, "gaps": gaps,
            "anchors": anchors, "bytes": total}, None, None


def plan(repository, *, scope="tests", output=None, include=(), members=(), limits=None, authority="standalone"):
    if authority != "standalone":
        _fail("Anvil-bound review authority is not implemented; use standalone")
    root = Path(repository).expanduser().resolve(strict=True)
    candidate = (Path(output).expanduser() if output else Path.home() / ".cache/repo-graph/reviews").resolve()
    destination = candidate
    if destination == root or root in destination.parents:
        _fail("review cache/output must be outside the source repository")
    destination = _cache(destination)
    limits = _limits(limits)
    inventory_coverage = {}
    files = repo_files(root, coverage=inventory_coverage)
    scope = _relative(scope) if scope != "." else "."
    prefix = "" if scope == "." else scope.rstrip("/") + "/"
    requested = [path for path in files if _is_test(path) and (not prefix or path.startswith(prefix) or path == scope)]
    explicit = []
    for path in include:
        path = _relative(str(path))
        if _sensitive(path):
            _fail("include path is excluded from source export policy")
        if path not in files:
            _fail(f"include path is not an eligible repository file: {path}")
        explicit.append(path)
    member_paths = []
    if members:
        if scope not in files or not _is_test(scope):
            _fail("members require --scope naming one eligible test file")
        for path in members:
            path = _relative(str(path))
            if _sensitive(path) or path not in files or not _is_test(path):
                _fail(f"member path is not an eligible nonsensitive test file: {path}")
            member_paths.append(path)
        member_paths = [scope, *sorted(set(member_paths) - {scope})]
        requested = member_paths
        selected = [scope]
    else:
        selected = requested[:min(limits["files"], limits["packets"])]
    inventory_blocked = [] if member_paths else requested[len(selected):]
    failed_scope = [{"path": item["path"], "reason": "inventory_unreadable"} for item in inventory_coverage.get("failures", [])
                    if _is_test(item.get("path", "")) and (not prefix or item["path"].startswith(prefix) or item["path"] == scope)]
    packet_specs, blocked = [], failed_scope + [{"path": path, "reason": "packet_limit" if index >= limits["packets"] else "inventory_limit"}
                                for index, path in enumerate(inventory_blocked, len(selected))]
    source_bytes = 0
    with SourceRoot(root) as source:
        binding = source.identity
        if member_paths and len(member_paths) > limits["files"]:
            blocked.extend({"path": path, "reason": "group_file_limit"} for path in member_paths)
        elif member_paths:
            used = [source_bytes]
            spec, failed_member, reason = _group_spec(source, files, member_paths, explicit, limits, used)
            source_bytes = used[0]
            if spec is None:
                blocked.extend({"path": path, "reason": f"group_blocked:{failed_member}:{reason}"} for path in member_paths)
            else:
                packet_specs.append(spec)
        for test in ([] if member_paths else selected):
            if len(packet_specs) >= limits["packets"]:
                blocked.append({"path": test, "reason": "packet_limit"}); continue
            if _sensitive(test):
                blocked.append({"path": test, "reason": "sensitive_source_excluded"}); continue
            try:
                info = source.info(test)
            except OSError:
                blocked.append({"path": test, "reason": "source_unavailable"}); continue
            if source_bytes + info.st_size > limits["source_bytes"]:
                blocked.append({"path": test, "reason": "source_read_budget"})
                blocked.extend({"path": path, "reason": "source_read_budget"} for path in selected[selected.index(test) + 1:])
                break
            primary, problem = _read_complete(source, test, limits["file_bytes"], remaining=limits["source_bytes"] - source_bytes)
            if problem:
                source_bytes += problem.get("read_bytes", 0)
                blocked.append(problem); continue
            source_bytes += primary["bytes"]
            try:
                tests, assertions, anchor_gaps = _anchors(test, primary["text"])
            except SyntaxError as error:
                blocked.append({"path": test, "reason": f"syntax_error:{error.lineno}"}); continue
            imports, unresolved = _targets(test, set(files), primary["text"])
            unresolved_gaps = [f"unresolved_import:{name}" for name in unresolved]
            primary_gaps = ["dynamic fixtures, plugins, parametrization and callers may be unresolved", CLOSURE_GAP, *anchor_gaps, *unresolved_gaps]
            dependencies = _ancestors(test, set(files)) + explicit
            dependencies += _package_inits(test, set(files)) + imports
            if len(_json(_packet_payload(test, [primary], primary_gaps, {"tests": tests, "assertions": assertions}, primary["bytes"], "0" * 64)).encode()) > limits["packet_bytes"]:
                units = _unit_fragments(test, primary)
                if not units or any(unit["bytes"] > limits["packet_bytes"] for unit in units):
                    blocked.append({"path": test, "reason": "oversized_unsplittable_logical_unit"}); continue
                blocked.append({"path": test, "reason": "partial_logical_split"})
                used = [source_bytes]
                for unit in units:
                    if len(packet_specs) >= limits["packets"]:
                        blocked.append({"path": test, "reason": "packet_limit"}); break
                    fragment = dict(primary, text=unit["text"], fragment={key: unit[key] for key in ("start_line", "end_line", "sha256", "bytes")})
                    uncovered = [f"uncovered_source_unit:{test}:{start}:{end}" for start, end in [(1, unit["start_line"] - 1), (unit["end_line"] + 1, primary["text"].count("\n") + 1)] if start <= end]
                    unit_assertions = [value for value in assertions if unit["start_line"] <= int(value[len(test) + 1:].split(":", 1)[0]) <= unit["end_line"]]
                    anchors = {"tests": [row for row in tests if unit["start_line"] <= row["line"] <= unit["end_line"]], "assertions": unit_assertions}
                    gaps = [*primary_gaps, *uncovered]
                    if len(_json(_packet_payload(test, [fragment], gaps, anchors, unit["bytes"], "0" * 64)).encode()) > limits["packet_bytes"]:
                        blocked.append({"path": test, "reason": "oversized_unsplittable_logical_unit"}); continue
                    closure, closure_gaps = _local_closure(source, files, sorted(set(dependencies) - {test}), limits, used, {"test": test, "sources": [fragment], "gaps": gaps, "anchors": anchors}, {test})
                    sources = [fragment, *closure]
                    packet_specs.append({"test": test, "sources": sources, "gaps": sorted(set([*gaps, *closure_gaps])), "anchors": anchors,
                                         "bytes": sum(item.get("fragment", {}).get("bytes", item["bytes"]) for item in sources)})
                source_bytes = used[0]
                continue
            sources, gaps, total = [primary], list(primary_gaps), primary["bytes"]
            # Exact payload sizing admits dependencies before publication.
            used = [source_bytes]
            closure, closure_gaps = _local_closure(source, files, sorted(set(dependencies) - {test}), limits, used, {"test": test, "sources": sources, "gaps": gaps, "anchors": {"tests": tests, "assertions": assertions}}, {test})
            source_bytes = used[0]
            for candidate in closure:
                sources.append(candidate); total += candidate["bytes"]
            gaps.extend(closure_gaps)
            packet_specs.append({"test": test, "sources": sources, "gaps": sorted(set(gaps)), "anchors": {"tests": tests, "assertions": assertions}, "bytes": total})
    scope_rows = []
    for spec in packet_specs:
        sources = [{k: value for k, value in item.items() if k != "text"} | {"start_line": item.get("fragment", {}).get("start_line", 1), "end_line": item.get("fragment", {}).get("end_line", item["text"].count("\n") + 1)} for item in spec["sources"]]
        scope_rows.append({"test": spec["test"], "test_members": spec.get("test_members", [spec["test"]]), "sources": sources, "gaps": spec["gaps"], "anchors": spec["anchors"], "bytes": spec["bytes"],
                           "review_scope": "materialized_source_only", "dependencies_complete": False})
    profile = "test-consolidation"
    scope_digest = _digest({"construction": PACKET_CONSTRUCTION, "profile": profile, "scope": scope, "members": member_paths, "packets": scope_rows, "include": sorted(set(explicit)), "limits": limits,
                            "blocked": blocked, "source_bytes": source_bytes})
    campaign_id = _digest({"schema": SCHEMA, "construction": PACKET_CONSTRUCTION, "profile": profile, "scope": scope, "scope_digest": scope_digest, "authority": authority})[:24]
    campaign_dir = destination / campaign_id
    campaign_dir.mkdir(mode=0o700, exist_ok=True)
    with _locked(campaign_dir) as boundary:
        try:
            existing = _load(boundary)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing["repository"]["binding"] != binding or existing["scope_digest"] != scope_digest:
                _fail("campaign id is bound to a different local repository snapshot")
            for reference in existing["packets"]:
                if reference["state"] == "stale": continue
                packet = _load_packet(boundary, reference)
                valid, reason = _source_ok(existing, packet)
                if valid and packet.get("result"):
                    try: _result_artifact(boundary, existing, packet)
                    except ValueError: valid, reason = False, "evidence_invalid"
                if not valid:
                    packet["state"] = "stale"; packet["stale_reason"] = reason; _save_packet(boundary, reference, packet)
            _save(boundary, existing)
            return {"campaign": str(campaign_dir), "campaign_id": campaign_id, "counts": existing["counts"], "resumed": True}
        packets = []
        for spec in packet_specs:
            sources = [{k: value for k, value in item.items() if k != "text"} | {"start_line": item.get("fragment", {}).get("start_line", 1), "end_line": item.get("fragment", {}).get("end_line", item["text"].count("\n") + 1)} for item in spec["sources"]]
            basis = _packet_basis(_packet_payload(spec["test"], spec["sources"], spec["gaps"], spec["anchors"], spec["bytes"], scope_digest, test_members=spec.get("test_members")))
            basis.pop("packet_id")
            packet_id = _digest({"construction": PACKET_CONSTRUCTION, "basis": basis})[:24]
            packet = _packet_payload(spec["test"], spec["sources"], spec["gaps"], spec["anchors"], spec["bytes"], scope_digest, packet_id, test_members=spec.get("test_members"))
            if len(_json(packet).encode()) > limits["packet_bytes"]:
                blocked.extend({"path": path, "reason": "packet_serialized_budget"}
                               for path in spec.get("test_members", [spec["test"]]))
                continue
            name = f"packet-{packet_id}.json"
            immutable = {key: value for key, value in packet.items() if key not in {"state", "attempts", "result", "decision", "stale_reason"}}
            boundary.write_json(name, immutable)
            packets.append({"packet_id": packet_id, "path": name, "payload_sha256": packet["payload_sha256"], "test": spec["test"], "test_members": packet["test_members"], "state": "planned", "gaps": spec["gaps"],
                            "attempts": [], "result": None, "decision": None})
        failed_total = inventory_coverage.get("failed", 0)
        failed_known = len(inventory_coverage.get("failures", []))
        requested_failed = len(failed_scope)
        partial = {item["path"] for item in blocked if item["reason"] == "partial_logical_split"}
        eligible_members = {member for item in packets for member in item.get("test_members", [item["test"]])}
        counts = {"inventory": len(files) + failed_total, "requested": len(requested) + requested_failed, "requested_count_knowledge": "exact" if failed_total == failed_known else "lower_bound", "eligible": len(eligible_members - partial), "excluded": len(files) - len(requested), "blocked": len({item["path"] for item in blocked}), "blocked_reasons": len(blocked), "packets": len(packets)}
        campaign = {"schema": SCHEMA, "campaign_id": campaign_id, "profile": profile, "authority": authority, "repository": {"path": str(root), "binding": binding}, "scope": scope, "scope_digest": scope_digest, "limits": limits, "review_scope": "materialized_source_only", "dependencies_complete": False,
                    "inventory": {"coverage": inventory_coverage, "requested": requested, "excluded_reason": "outside_tests_scope", "blocked": blocked}, "counts": counts, "packets": packets}
        _save(boundary, campaign)
    return {"campaign": str(campaign_dir), "campaign_id": campaign_id, "counts": counts, "resumed": False}


def _packet(campaign, packet_id):
    return next((item for item in campaign["packets"] if item["packet_id"] == packet_id), None)


def next_packet(campaign, *, worker="codex"):
    path = _campaign_path(campaign)
    if not isinstance(worker, str) or not worker or len(worker) > 256:
        _fail("worker identity required")
    with _locked(path) as boundary:
        state = _load(boundary)
        for reference in state["packets"]:
            if reference["state"] != "planned":
                continue
            packet = _load_packet(boundary, reference)
            valid, reason = _source_ok(state, packet)
            if not valid:
                packet["state"] = "stale"; packet["stale_reason"] = reason; _save_packet(boundary, reference, packet); continue
            attempt = {"attempt_id": uuid.uuid4().hex, "worker_id": worker, "state": "assigned", "assigned_at": time.time(), "outcome": None}
            packet["attempts"].append(attempt); packet["state"] = "assigned"
            _save_packet(boundary, reference, packet)
            _save(boundary, state)
            return {"campaign_id": state["campaign_id"], "packet": packet, "attempt": attempt, "result_schema": result_schema(state["limits"]["result_bytes"])}
        _save(boundary, state)
        return {"campaign_id": state["campaign_id"], "packet": None, "reason": "no_eligible_packet", "result_schema": result_schema(state["limits"]["result_bytes"])}


def _validate_result(packet, result, maximum):
    if type(result) is not dict:
        _fail("result must be a JSON object")
    if len(_json(result).encode()) > maximum:
        _fail("result exceeds campaign result byte budget")
    required = {"packet_id", "attempt_id", "worker_id", "reviewed_ranges", "findings", "assertion_map", "outcome"}
    if set(result) - required or required - set(result):
        _fail("worker result has unknown or missing fields")
    if type(result["outcome"]) is not str or result["outcome"] not in {"completed", "needs_source", "uncertain", "cancelled"}:
        _fail("invalid worker outcome")
    if not all(isinstance(result[key], str) and result[key] for key in ("packet_id", "attempt_id", "worker_id")):
        _fail("result identity fields must be non-empty strings")
    if type(result["reviewed_ranges"]) is not list or type(result["findings"]) is not list or type(result["assertion_map"]) is not list:
        _fail("result collections must be lists")
    if len(result["findings"]) > MAX_FINDINGS:
        _fail("too many findings")
    source = {item["path"]: item for item in packet["sources"]}
    for entry in result["reviewed_ranges"]:
        if type(entry) is not dict or set(entry) != {"path", "start_line", "end_line", "sha256"} or entry["path"] not in source:
            _fail("invalid reviewed range")
        item = source[entry["path"]]
        if type(entry["start_line"]) is not int or type(entry["end_line"]) is not int or entry["sha256"] != item["sha256"] or not (item["start_line"] <= entry["start_line"] <= entry["end_line"] <= item["end_line"]):
            _fail("reviewed range is outside packet identity")
    for finding in result["findings"]:
        if type(finding) is not dict or set(finding) != {"summary", "citations"} or not isinstance(finding["summary"], str) or len(finding["summary"]) > MAX_TEXT:
            _fail("invalid finding")
        if type(finding["citations"]) is not list or not finding["citations"] or len(finding["citations"]) > 20:
            _fail("finding citations required")
        for citation in finding["citations"]:
            _validate_result(packet, {"packet_id": result["packet_id"], "attempt_id": result["attempt_id"], "worker_id": result["worker_id"], "reviewed_ranges": [citation], "findings": [], "assertion_map": [], "outcome": "needs_source"}, maximum)
    allowed = {"preserved", "combined", "duplicate_with_evidence", "unresolved"}
    for item in result["assertion_map"]:
        if type(item) is not dict or set(item) != {"original", "disposition", "evidence"} or type(item["disposition"]) is not str or item["disposition"] not in allowed:
            _fail("invalid assertion map")
        if not all(isinstance(item[key], str) and len(item[key]) <= MAX_TEXT for key in ("original", "evidence")):
            _fail("invalid assertion map text")
    if result["outcome"] == "completed":
        required_ranges = {(item["path"], item["start_line"], item["end_line"], item["sha256"]) for item in packet["sources"]}
        actual_ranges = {(item["path"], item["start_line"], item["end_line"], item["sha256"]) for item in result["reviewed_ranges"]}
        if actual_ranges != required_ranges:
            _fail("completed result must cover every materialized packet source")
        expected = set(packet["anchors"]["assertions"])
        actual = [item["original"] for item in result["assertion_map"]]
        if len(actual) != len(set(actual)) or set(actual) != expected or any(item["disposition"] == "unresolved" for item in result["assertion_map"]):
            _fail("completed result must account for each original assertion without unresolved behavior")
    return _digest(result)


def record(campaign, result):
    path = _campaign_path(campaign)
    if isinstance(result, (str, Path)):
        value = Path(result).expanduser().resolve()
        with SourceRoot(value.parent) as root:
            raw, _, _ = root.read(value.name, MAX_RESULT_BYTES, max_bytes=MAX_RESULT_BYTES)
        result = _parse_json(raw, "result")
    with _locked(path) as boundary:
        state = _load(boundary)
        if type(result) is dict and result.get("kind") == "independent_decision":
            required = {"kind", "packet_id", "attempt_id", "result_sha256", "reviewer_id", "disposition", "provenance", "rationale"}
            if set(result) != required or type(result["disposition"]) is not str or result["disposition"] not in {"accepted", "rejected", "needs_source"}:
                _fail("invalid independent decision")
            if type(result["provenance"]) is not dict or set(result["provenance"]) != {"model", "surface", "reasoning"} or not all(isinstance(value, str) and value for value in result["provenance"].values()) or not isinstance(result["rationale"], str) or not result["rationale"] or len(result["rationale"]) > MAX_TEXT:
                _fail("independent decision provenance and rationale required")
            if not all(isinstance(result[key], str) and result[key] for key in ("packet_id", "attempt_id", "result_sha256", "reviewer_id")):
                _fail("independent decision identity fields required")
            if len(_json(result).encode()) > state["limits"]["result_bytes"] or any(len(result[key]) > 256 for key in ("packet_id", "attempt_id", "result_sha256", "reviewer_id")) or any(len(value) > 256 for value in result["provenance"].values()):
                _fail("independent decision exceeds bounded record limits")
            reference = _packet(state, result["packet_id"])
            packet = _load_packet(boundary, reference) if reference else None
            if not packet or not packet["result"] or packet["result"]["sha256"] != result["result_sha256"]:
                _fail("decision does not bind an exact recorded result")
            attempt = next((item for item in packet["attempts"] if item["attempt_id"] == result["attempt_id"]), None)
            if not attempt or result["reviewer_id"] == attempt["worker_id"]:
                _fail("independent reviewer must differ from worker")
            stored = _result_artifact(boundary, state, packet)
            if _digest(stored) != result["result_sha256"]: _fail("recorded result digest mismatch")
            valid, reason = _source_ok(state, packet)
            if not valid:
                packet["state"] = "stale"; packet["stale_reason"] = reason; _save_packet(boundary, reference, packet); _save(boundary, state)
                return {"state": "stale", "reason": reason}
            if packet["decision"] == result:
                return {"state": packet["state"], "idempotent": True}
            if attempt["outcome"] != "completed" or packet["state"] != "validated":
                _fail("only a completed validated result may be independently accepted")
            if packet["decision"] and packet["decision"] != result:
                _fail("conflicting independent decision")
            idempotent = packet["decision"] == result
            packet["decision"] = result; packet["state"] = result["disposition"]
            _save_packet(boundary, reference, packet); _save(boundary, state); return {"state": packet["state"], "idempotent": idempotent}
        reference = _packet(state, result.get("packet_id") if type(result) is dict else None)
        packet = _load_packet(boundary, reference) if reference else None
        if not packet:
            _fail("unknown packet")
        attempt = next((item for item in packet["attempts"] if item["attempt_id"] == result.get("attempt_id")), None)
        if not attempt or attempt["worker_id"] != result.get("worker_id"):
            _fail("result is not assigned to this worker attempt")
        valid, reason = _source_ok(state, packet)
        if not valid:
            packet["state"] = "stale"; packet["stale_reason"] = reason; _save_packet(boundary, reference, packet); _save(boundary, state); return {"state": "stale", "reason": reason}
        digest = _validate_result(packet, result, state["limits"]["result_bytes"])
        if packet["result"]:
            if packet["result"]["sha256"] != digest:
                _fail("conflicting result for an immutable attempt")
            try:
                _result_artifact(boundary, state, packet)
            except ValueError:
                packet["state"] = "stale"; packet["stale_reason"] = "evidence_invalid"; _save_packet(boundary, reference, packet); _save(boundary, state)
                return {"state": "stale", "reason": "evidence_invalid"}
            return {"state": packet["state"], "sha256": digest, "idempotent": True}
        boundary.write_json(f"result-{digest}.json", result)
        packet["result"] = {"sha256": digest, "path": f"result-{digest}.json"}; packet["state"] = "validated" if result["outcome"] == "completed" else result["outcome"]; attempt["state"] = packet["state"]; attempt["outcome"] = result["outcome"]
        _save_packet(boundary, reference, packet)
        _save(boundary, state)
        return {"state": packet["state"], "sha256": digest, "idempotent": False}


def status(campaign, *, offset=0, limit=20):
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
        _fail("offset must be non-negative and limit must be 1..100")
    path = _campaign_path(campaign)
    with _locked(path) as boundary:
        state = _load(boundary)
        observed = state["packets"][offset:offset + limit]
        for reference in observed:
            packet = _load_packet(boundary, reference)
            valid, reason = _source_ok(state, packet)
            if valid and packet.get("result"):
                try: _result_artifact(boundary, state, packet)
                except ValueError: valid, reason = False, "evidence_invalid"
            if not valid and packet["state"] != "stale":
                packet["state"] = "stale"; packet["stale_reason"] = reason; _save_packet(boundary, reference, packet)
        _save(boundary, state)
    rows = [{"packet_id": item["packet_id"], "test": item["test"], "test_members": item.get("test_members", [item["test"]]), "state": item["state"],
             "stale_reason": item.get("stale_reason"), "gaps": item["gaps"][:20],
             "gap_count": len(item["gaps"]), "gaps_truncated": len(item["gaps"]) > 20,
             "attempts": len(item["attempts"]), "result": item["result"]} for item in state["packets"]]
    states = {}
    for row in rows: states[row["state"]] = states.get(row["state"], 0) + 1
    checked_all = offset == 0 and limit >= len(rows)
    coverage = state["inventory"]["coverage"]
    return {"campaign_id": state["campaign_id"], "authority": state["authority"], "review_scope": state["review_scope"], "dependencies_complete": False, "counts": state["counts"], "state_counts": states, "inventory_failures": {"total": coverage.get("failed", 0), "sample": coverage.get("failures", [])[:50], "truncated": coverage.get("failed", 0) > len(coverage.get("failures", []))}, "inventory_blocked": state["inventory"]["blocked"][:100], "blocked_truncated": len(state["inventory"]["blocked"]) > 100, "packets": rows[offset:offset + limit], "offset": offset, "freshness": "checked" if checked_all else "checked_page_only", "unobserved_freshness": 0 if checked_all else len(rows) - len(rows[offset:offset + limit]), "next_offset": offset + limit if offset + limit < len(rows) else None}
