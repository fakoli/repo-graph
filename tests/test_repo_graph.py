from __future__ import annotations

from io import BytesIO
from contextlib import redirect_stdout
from io import StringIO
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import subprocess
from repo_graph.source import SourceRoot
from repo_graph import source as source_module


SCRIPT = Path(__file__).resolve().parents[1] / "repo_graph/builder.py"
SPEC = importlib.util.spec_from_file_location("build_repo_graph", SCRIPT)
assert SPEC and SPEC.loader
repo_graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repo_graph)


class RepoGraphTests(unittest.TestCase):
    def test_unsupported_safe_reads_keep_metadata_map_and_refuse_source_bytes(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir()
            out = Path(scratch) / 'out'
            (root / 'main.py').write_text('def privateBody(): pass\n')
            with patch.object(source_module, 'DESCRIPTOR_OPENS', False), redirect_stdout(StringIO()):
                with SourceRoot(root) as boundary:
                    self.assertGreater(boundary.info('main.py').st_size, 0)
                    with self.assertRaises(OSError): boundary.read('main.py', 1024)
                self.assertEqual(repo_graph.main([str(root), '--output', str(out)]), 0)
            graph = json.loads((out / 'graph.json').read_text())
            self.assertEqual(graph['files'], ['main.py'])
            self.assertFalse(graph['scan']['secure_reads'])
            self.assertEqual(graph['search']['status'], 'unavailable')
            self.assertTrue((out / 'architecture.html').is_file())
            self.assertFalse((out / 'search.db').exists())

    @unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'), 'Descriptor-relative opens unavailable')
    def test_root_acquisition_rejects_ancestor_swap_after_resolution(self):
        with tempfile.TemporaryDirectory() as scratch:
            parent = Path(scratch) / 'owner'; root = parent / 'repo'; root.mkdir(parents=True)
            outside = Path(scratch) / 'outside'; (outside / 'repo').mkdir(parents=True)
            (outside / 'repo/main.py').write_text('outsideSentinel')
            original_resolve = Path.resolve
            def swap(path, *args, **kwargs):
                resolved = original_resolve(path, *args, **kwargs)
                if path == root:
                    parent.rename(Path(scratch) / 'original-owner')
                    parent.symlink_to(outside, target_is_directory=True)
                return resolved
            with patch.object(Path, 'resolve', swap), self.assertRaises(OSError):
                with SourceRoot(root) as boundary: boundary.read('main.py', 1024)

    def test_inventory_imports_and_manifest_reject_ancestor_symlinks_even_with_warm_cache(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir()
            outside = Path(scratch) / 'outside'; outside.mkdir()
            (root / 'api').mkdir(); (root / 'store').mkdir()
            (root / 'api/main.go').write_text('package api\nimport "example.test/project/store"\n')
            (root / 'store/store.go').write_text('package store\n')
            (root / 'go.mod').write_text('module example.test/project\n')
            (outside / 'main.go').write_text('package api\nimport "example.test/project/store"\n')
            (outside / 'module').write_text('module example.test/project\n')
            files = ['api/main.go', 'store/store.go', 'go.mod']
            cache = Path(scratch) / 'cache.json'; tree = repo_graph.tree_index(files)
            self.assertTrue(repo_graph.extract_dependencies(root, files, tree, cache)[0])
            (root / 'api/main.go').unlink(); (root / 'api').rmdir()
            (root / 'api').symlink_to(outside, target_is_directory=True)
            (root / 'go.mod').unlink(); (root / 'go.mod').symlink_to(outside / 'module')
            listed = subprocess.CompletedProcess([], 0, b'api/main.go\0store/store.go\0go.mod\0')
            coverage = {}
            with patch.object(repo_graph.subprocess, 'run', return_value=listed):
                self.assertEqual(repo_graph.repo_files(root, coverage=coverage), ['store/store.go'])
            self.assertEqual(coverage['failed'], 2)
            edges, result = repo_graph.extract_dependencies(root, files, tree, cache)
            self.assertEqual(edges, [])
            self.assertEqual(result['code_files'], 1)
            self.assertEqual({r['path'] for r in result['failures']}, {'api/main.go', 'go.mod'})

    def test_import_cache_identity_uses_root_content_config_and_version(self):
        with tempfile.TemporaryDirectory() as scratch:
            roots = [Path(scratch) / name for name in ('one', 'two')]
            files = ['api/main.py', 'store/item.py', 'cache/item.py']
            for root, target in zip(roots, ('store', 'cache')):
                for name in ('api', 'store', 'cache'): (root / name).mkdir(parents=True)
                (root / files[0]).write_text(f'from {target} import item\n')
                for name in files[1:]: (root / name).write_text('')
                for name in files: os.utime(root / name, ns=(10**15, 10**15))
            cache = Path(scratch) / 'scan.json'; tree = repo_graph.tree_index(files)
            first, _ = repo_graph.extract_dependencies(roots[0], files, tree, cache)
            second, changed_root = repo_graph.extract_dependencies(roots[1], files, tree, cache)
            self.assertEqual(first[0]['target'], 'store')
            self.assertEqual(second[0]['target'], 'cache')
            self.assertEqual(changed_root['reused'], 0)
            (roots[1] / files[0]).write_text('from store import item\n')
            os.utime(roots[1] / files[0], ns=(10**15, 10**15))
            third, changed_content = repo_graph.extract_dependencies(roots[1], files, tree, cache)
            self.assertEqual(third[0]['target'], 'store')
            self.assertEqual(changed_content['scanned'], 1)
            with patch.object(repo_graph, 'READ_LIMIT', repo_graph.READ_LIMIT - 1):
                self.assertEqual(repo_graph.extract_dependencies(roots[1], files, tree, cache)[1]['reused'], 0)
            cache.write_text(json.dumps({files[0]: [10**15, 23, ['cache']]}))
            self.assertEqual(repo_graph.extract_dependencies(roots[1], files, tree, cache)[0][0]['target'], 'store')
            preserved = cache.read_bytes()
            with patch.object(repo_graph.os, 'replace', side_effect=OSError('synthetic interruption')):
                with self.assertRaises(OSError): repo_graph.extract_dependencies(roots[1], files, tree, cache)
            self.assertEqual(cache.read_bytes(), preserved)

    @unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'), 'Descriptor-relative opens unavailable')
    def test_import_read_pins_directory_during_ancestor_swap(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir()
            outside = Path(scratch) / 'outside'; outside.mkdir()
            (root / 'api').mkdir(); (root / 'store').mkdir(); (root / 'evil').mkdir()
            (root / 'api/main.py').write_text('from store import item\n')
            (outside / 'main.py').write_text('from evil import item\n')
            files = ['api/main.py', 'store/item.py', 'evil/item.py']
            original_open = os.open
            def swap(path, flags, *args, **kwargs):
                if path == 'main.py':
                    (root / 'api').rename(root / 'original')
                    (root / 'api').symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)
            with SourceRoot(root) as bound:
                with patch.object(repo_graph, 'SourceRoot', side_effect=lambda owner: bound if owner == root else SourceRoot(owner)), patch.object(source_module.os, 'open', side_effect=swap):
                    edges, _ = repo_graph.extract_dependencies(root, files, repo_graph.tree_index(files), Path(scratch) / 'scan.json')
            self.assertEqual(edges[0]['target'], 'store')

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
