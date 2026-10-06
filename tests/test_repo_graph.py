from __future__ import annotations

from io import BytesIO
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os


SCRIPT = Path(__file__).resolve().parents[1] / "repo_graph/builder.py"
SPEC = importlib.util.spec_from_file_location("build_repo_graph", SCRIPT)
assert SPEC and SPEC.loader
repo_graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repo_graph)


class RepoGraphTests(unittest.TestCase):
    def test_go_links_tree_and_incremental_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go.mod").write_text("module example.test/project\n", encoding="utf-8")
            for name in ("api", "store"):
                (root / name).mkdir()
            (root / "api" / "main.go").write_text(
                'package api\nimport (\n "example.test/project/store"\n "fmt"\n)\n',
                encoding="utf-8")
            (root / "store" / "store.go").write_text("package store\n", encoding="utf-8")
            (root / ".env").write_text("secret", encoding="utf-8")
            files = repo_graph.repo_files(root)
            self.assertNotIn(".env", files)
            tree = repo_graph.tree_index(files)
            self.assertEqual(tree[""]["count"], len(files))
            self.assertEqual(tree["api"]["count"], 1)
            cache = root / ".scan-cache.json"
            edges, first = repo_graph.extract_dependencies(root, files, tree, cache)
            self.assertEqual(edges, [{"source": "api", "target": "store", "count": 1, "relation": "imports"}])
            self.assertEqual(repo_graph.scope_edges(edges)[""][0]["target"], "store")
            self.assertEqual(
                repo_graph.scope_edges([{"source": "", "target": "api", "count": 1}])[""][0]["source"],
                "scope:")
            self.assertEqual(first["scanned"], 2)
            self.assertEqual(repo_graph.extract_dependencies(root, files, tree, cache)[1]["reused"], 2)

    def test_unresolved_relative_imports_do_not_create_false_edges(self) -> None:
        tree = repo_graph.tree_index(['src/app.ts','src/store.ts','pkg/main.py'])
        self.assertEqual(repo_graph.local_target('src/app.ts','./store','',tree),'src')
        self.assertIsNone(repo_graph.local_target('src/app.ts','./missing','',tree))
        self.assertIsNone(repo_graph.local_target('pkg/main.py','unknown_package','',tree))

    def test_large_tree_keeps_every_directory_and_escapes_html(self) -> None:
        files = [f"internal/service/s{i:03}/resource.go" for i in range(230)]
        tree = repo_graph.tree_index(files)
        self.assertEqual(len(tree["internal/service"]["children"]), 230)
        self.assertEqual(tree["internal/service"]["count"], 230)
        source = repo_graph.mermaid(tree, {})
        self.assertLessEqual(source.count('["'), repo_graph.PAGE_SIZE)
        self.assertLessEqual(source.count("-->") + source.count("-.->"), 40)
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "architecture.html"
            repo_graph.write_page(page, {"name": "</script><script>alert(1)</script>", "tree": tree})
            html = page.read_text(encoding="utf-8")
        self.assertNotIn("</script><script>alert(1)", html)
        self.assertIn("u003c/script", html)
        self.assertNotIn("__VIEW_HELPERS__", html)

    def test_system_partition_preserves_files_and_imports(self) -> None:
        files = ["main.go", "internal/core.go", "internal/provider/provider.go", "docs/guide.md", "website/index.md"]
        files += [f"internal/service{i}/resource.go" for i in range(30)]
        files += [f"area{i}/data.txt" for i in range(20)]
        tree = repo_graph.tree_index(files)
        system = repo_graph.system_view(tree, [
            {"source": "", "target": "internal/service0", "count": 3},
            {"source": "internal/service0", "target": "internal/service29", "count": 2},
        ], {})
        self.assertLessEqual(len(system["nodes"]), 12)
        self.assertEqual(sum(node["count"] for node in system["nodes"]), len(files))
        paths = [path for node in system["nodes"] for path in node["paths"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(sum(edge["count"] for edge in system["edges"]), 5)
        self.assertTrue(all(node["paths"] for node in system["nodes"]))
        self.assertTrue(any(node["paths"] == ["internal/provider"] for node in system["nodes"]))

    def test_jev_uses_one_bounded_typed_request_and_confidence_gate(self) -> None:
        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        captured = {}
        def fake_open(request, timeout):
            captured["request"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response(json.dumps({"model":repo_graph.jev.MODEL, "usage":{"input_tokens":100,"output_tokens":10}, "answers": {
                "c0": {"type": "choice", "choice": "documentation", "confidence": .91},
                "c1": {"type": "choice", "choice": "library", "confidence": .2},
            }}).encode())
        with patch.object(repo_graph.jev, "OPEN", fake_open):
            roles = repo_graph.jev_roles(["docs", "src"], "synthetic-key")
        self.assertEqual(roles, {"docs": "documentation"})
        self.assertEqual(captured["request"]["state"], {"directories": ["docs", "src"]})
        self.assertEqual(len(captured["request"]["questions"]), 2)
        self.assertEqual(captured["timeout"], 3)

    def test_key_loader_reads_only_named_entry_without_sourcing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".env").write_text(
                "IGNORED_KEY=synthetic-other-value\nexport TYPESAFE_API_KEY='synthetic-test-key'\n",
                encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch.object(repo_graph.Path, "home", return_value=home):
                self.assertEqual(repo_graph.typesafe_key(), "synthetic-test-key")


if __name__ == "__main__":
    unittest.main()
