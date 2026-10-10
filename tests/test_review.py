import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

from repo_graph import review


class ReviewTests(unittest.TestCase):
    def make_repo(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / "source"; root.mkdir()
        (root / "tests").mkdir(); (root / "pkg").mkdir()
        (root / "tests" / "conftest.py").write_text("def fixture(): return 1\n")
        (root / "pkg" / "__init__.py").write_text("from .helper import value\n")
        (root / "pkg" / "helper.py").write_text("from .extra import marker\ndef value(): return marker()\n")
        (root / "pkg" / "extra.py").write_text("from .helper import value\ndef marker(): return 1\n")
        (root / "tests" / "test_one.py").write_text("from pkg.helper import value\ndef test_value():\n    assert value() == 1\n")
        return temp, root

    def result_for(self, assigned, *, outcome="completed"):
        packet, attempt = assigned["packet"], assigned["attempt"]
        ranges = []
        for source in packet["sources"]:
            range_ = {key: source[key] for key in ("path", "sha256")}
            range_.update(start_line=source["start_line"], end_line=source["end_line"]); ranges.append(range_)
        return {"packet_id": packet["packet_id"], "attempt_id": attempt["attempt_id"], "worker_id": attempt["worker_id"],
                "reviewed_ranges": ranges, "findings": [{"summary": "keeps distinct behavior", "citations": [ranges[0]]}],
                "assertion_map": [{"original": anchor, "disposition": "preserved", "evidence": "assertion retained"} for anchor in packet["anchors"]["assertions"]], "outcome": outcome}

    def test_journey_resume_and_independent_acceptance(self):
        temp, root = self.make_repo()
        with temp:
            output = Path(temp.name) / "cache"
            created = review.plan(root, output=output)
            resumed = review.plan(root, output=output)
            self.assertTrue(resumed["resumed"])
            assigned = review.next_packet(created["campaign"], worker="codex-1")
            self.assertIn("worker_result", assigned["result_schema"])
            self.assertEqual({"tests/test_one.py", "tests/conftest.py", "pkg/__init__.py", "pkg/helper.py", "pkg/extra.py"}, {source["path"] for source in assigned["packet"]["sources"]})
            result = self.result_for(assigned)
            saved = review.record(created["campaign"], result)
            self.assertEqual("validated", saved["state"])
            self.assertTrue(review.record(created["campaign"], result)["idempotent"])
            decision = {"kind": "independent_decision", "packet_id": result["packet_id"], "attempt_id": result["attempt_id"],
                        "result_sha256": saved["sha256"], "reviewer_id": "astra-2", "disposition": "accepted", "provenance": {"model": "astra", "surface": "codex", "reasoning": "high"}, "rationale": "independent source review"}
            self.assertEqual("accepted", review.record(created["campaign"], decision)["state"])
            self.assertTrue(review.record(created["campaign"], decision)["idempotent"])
            self.assertEqual("accepted", review.status(created["campaign"])["packets"][0]["state"])
            (Path(created["campaign"]) / f"result-{saved['sha256']}.json").unlink()
            stale = review.status(created["campaign"])["packets"][0]
            self.assertEqual("stale", stale["state"])
            self.assertEqual("evidence_invalid", stale["stale_reason"])

    def test_packet_integrity_and_interrupted_publication_do_not_dispatch(self):
        temp, root = self.make_repo()
        with temp:
            created = review.plan(root, output=Path(temp.name) / "cache")
            manifest = Path(created["campaign"]) / "campaign.json"; manifest_original = manifest.read_bytes()
            legacy = json.loads(manifest_original); legacy["schema"] = "repo-graph-review-v1"; manifest.write_text(json.dumps(legacy))
            with self.assertRaisesRegex(ValueError, "unsupported campaign schema"):
                review.next_packet(created["campaign"])
            manifest.write_bytes(manifest_original)
            packet_file = next(Path(created["campaign"]).glob("packet-*.json"))
            original = packet_file.read_bytes()
            altered = json.loads(original)
            altered["source_text"][0]["text"] += "# tampered\n"
            packet_file.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "payload identity"):
                review.next_packet(created["campaign"])
            altered = json.loads(original); altered["instructions"] = "unsealed"
            packet_file.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "immutable packet shape"):
                review.next_packet(created["campaign"])
            packet_file.write_bytes(original)
            assigned = review.next_packet(created["campaign"])
            result = self.result_for(assigned)
            with self.assertRaisesRegex(ValueError, "every materialized"):
                review.record(created["campaign"], dict(result, reviewed_ranges=result["reviewed_ranges"][:1]))
            with self.assertRaisesRegex(ValueError, "outcome"):
                review.record(created["campaign"], dict(result, outcome=[]))
            saved = review.record(created["campaign"], result)
            self.assertEqual("validated", saved["state"])
            (Path(created["campaign"]) / f"result-{saved['sha256']}.json").unlink()
            self.assertEqual("stale", review.record(created["campaign"], result)["state"])
            # The result blob is durable before the sole manifest publication. A
            # failed publication leaves the assigned manifest and permits only
            # idempotent receipt reconciliation, not a second dispatch.
            other = review.plan(root, output=Path(temp.name) / "retry-cache")
            pending = review.next_packet(other["campaign"]); pending_result = self.result_for(pending)
            with patch.object(review, "_save", side_effect=OSError("interrupted before manifest")):
                with self.assertRaises(OSError): review.record(other["campaign"], pending_result)
            self.assertIsNone(review.next_packet(other["campaign"])["packet"])
            self.assertEqual("validated", review.record(other["campaign"], pending_result)["state"])

    def test_source_change_and_uncertain_attempts_do_not_complete(self):
        temp, root = self.make_repo()
        with temp:
            created = review.plan(root, output=Path(temp.name) / "cache")
            assigned = review.next_packet(created["campaign"])
            (root / "tests" / "test_one.py").unlink()
            self.assertEqual("stale", review.record(created["campaign"], self.result_for(assigned, outcome="uncertain"))["state"])
            self.assertEqual("stale", review.status(created["campaign"])["packets"][0]["state"])

    def test_containment_budgets_and_oversized_units_are_visible(self):
        temp, root = self.make_repo()
        with temp:
            (root / "tests" / "test_large.py").write_text("#" * 300)
            (root / "tests" / "test_linked.py").symlink_to(root / "pkg" / "helper.py")
            created = review.plan(root, output=Path(temp.name) / "cache", limits={"file_bytes": 128})
            report = review.status(created["campaign"])
            self.assertGreaterEqual(report["counts"]["blocked"], 1)
            self.assertTrue(any(row["reason"] == "oversized" for row in report["inventory_blocked"]))
            self.assertEqual(3, report["counts"]["requested"])
            self.assertTrue(any(row["path"] == "tests/test_linked.py" for row in report["inventory_blocked"]))
            bounded = review.plan(root, output=Path(temp.name) / "other-cache", limits={"source_bytes": 1})
            self.assertGreaterEqual(review.status(bounded["campaign"])["counts"]["blocked"], 1)
            (root / "tests" / "test_split.py").write_text("blob = '" + "x" * 3000 + "'\n\ndef test_left():\n    assert True\n\n# second context\ndef test_right():\n    assert True\n")
            split = review.plan(root, output=Path(temp.name) / "split-cache", limits={"packet_bytes": 2000})
            self.assertGreaterEqual(split["counts"]["packets"], 2)
            assigned_packets = [review.next_packet(split["campaign"]) for _ in range(split["counts"]["packets"])]
            split_assigned = next(value for value in assigned_packets if value["packet"]["sources"][0].get("fragment"))
            first = split_assigned["packet"]
            self.assertIn("uncovered_source_unit", " ".join(first["gaps"]))
            invalid = self.result_for(split_assigned); invalid["reviewed_ranges"][0]["start_line"] = 1
            with self.assertRaises(ValueError): review.record(split["campaign"], invalid)
            stored = review.record(split["campaign"], self.result_for(split_assigned))
            decision = {"kind": "independent_decision", "packet_id": first["packet_id"], "attempt_id": split_assigned["attempt"]["attempt_id"], "result_sha256": stored["sha256"], "reviewer_id": "independent", "disposition": "accepted", "provenance": {"model": "other", "surface": "codex", "reasoning": "high"}, "rationale": "range checked"}
            self.assertEqual("accepted", review.record(split["campaign"], decision)["state"])
            (root / "tests" / "test_split.py").write_text("changed = True\n" + (root / "tests" / "test_split.py").read_text())
            self.assertIn("stale", [row["state"] for row in review.status(split["campaign"], limit=100)["packets"]])
            (root / "tests" / "test_fanout.py").write_text("\n".join(f"from pkg.dep{i} import value" for i in range(12)) + "\ndef test_fanout():\n    assert True\n")
            for i in range(12): (root / "pkg" / f"dep{i}.py").write_text("value = '" + "\\\"" * 300 + "'\n")
            fanout = review.plan(root, output=Path(temp.name) / "fanout-cache", limits={"packet_bytes": 1400, "source_bytes": 20_000})
            self.assertGreater(fanout["counts"]["packets"], 0)
            fanout_row = next(row for row in review.status(fanout["campaign"], limit=100)["packets"] if row["test"] == "tests/test_fanout.py")
            self.assertIn("closure_stopped:packet_budget;known_omissions>=1", " ".join(fanout_row["gaps"]))
            for packet_file in Path(fanout["campaign"]).glob("packet-*.json"):
                self.assertLessEqual(len(packet_file.read_bytes()), 1400)
            original_read = review.SourceRoot.read
            for kind, budget, packet_limit, count in (("primary", 100, review.MAX_PACKET_BYTES, 3),
                                                      ("dependencies", 180, review.MAX_PACKET_BYTES, 3),
                                                      ("gap_budget", 2000, 1500, 30)):
                bad_root = Path(temp.name) / kind; bad_root.mkdir()
                (bad_root / "tests").mkdir(); (bad_root / "pkg").mkdir()
                (bad_root / "pkg" / "__init__.py").write_text("")
                for index in range(count):
                    path = bad_root / (f"tests/test_{index}.py" if kind == "primary" else f"pkg/d{index}.py")
                    path.write_bytes(b"\xff" * 60)
                if kind != "primary":
                    (bad_root / "tests" / "test_invalid.py").write_text("from pkg import " + ", ".join(f"d{i}" for i in range(count)) + "\ndef test_ok():\n    assert True\n")
                streamed = [0]
                def measured(owner, path, *args, **kwargs):
                    row = kwargs.setdefault("measurements", {})
                    try: return original_read(owner, path, *args, **kwargs)
                    finally:
                        if owner.root == bad_root: streamed[0] += row.get("stream_bytes", 0)
                with patch.object(review.SourceRoot, "read", measured):
                    invalid = review.plan(bad_root, output=Path(temp.name) / f"{kind}-cache", limits={"source_bytes": budget, "packet_bytes": packet_limit})
                self.assertGreater(streamed[0], 0)
                self.assertLessEqual(streamed[0], budget, kind)
                status = review.status(invalid["campaign"])
                if kind == "primary":
                    self.assertTrue(any(row["reason"] == "source_read_budget" for row in status["inventory_blocked"]))
                else:
                    self.assertEqual(1, invalid["counts"]["packets"])
                    self.assertIn("non_utf8_source", " ".join(status["packets"][0]["gaps"]))
                if kind == "gap_budget":
                    self.assertIn("closure_stopped:gap_budget;known_omissions>=1", " ".join(status["packets"][0]["gaps"]))
                for packet_file in Path(invalid["campaign"]).glob("packet-*.json"):
                    self.assertLessEqual(len(packet_file.read_bytes()), packet_limit)
            _, anchors, gaps = review._anchors("tests/test_one.py", "class T:\n def test_x(self):\n  self.assertEqual(1, 1); self.assertTrue(True)\nwith pytest.raises(ValueError): pass\n")
            self.assertEqual(3, len(anchors)); self.assertTrue(all(anchor.startswith("tests/test_one.py:") for anchor in anchors)); self.assertEqual([], gaps)
            with self.assertRaises(ValueError):
                review.plan(root, output=root / "inside")
            self.assertFalse((root / "inside").exists())

    def test_conflicting_and_self_acceptance_are_refused(self):
        temp, root = self.make_repo()
        with temp:
            created = review.plan(root, output=Path(temp.name) / "cache")
            assigned = review.next_packet(created["campaign"], worker="same")
            result = self.result_for(assigned)
            saved = review.record(created["campaign"], result)
            with self.assertRaises(ValueError):
                review.record(created["campaign"], dict(result, outcome="needs_source"))
            with self.assertRaises(ValueError):
                review.record(created["campaign"], {"kind": "independent_decision", "packet_id": result["packet_id"], "attempt_id": result["attempt_id"],
                    "result_sha256": saved["sha256"], "reviewer_id": "same", "disposition": "accepted", "provenance": {"model": "same", "surface": "codex", "reasoning": "high"}, "rationale": "no"})

    def test_crashed_directory_lock_releases_without_replaying_an_attempt(self):
        temp, root = self.make_repo()
        with temp:
            created = review.plan(root, output=Path(temp.name) / "cache")
            holder_script = "from repo_graph.review import _locked; from pathlib import Path; import sys,time\nwith _locked(Path(sys.argv[1])): print('locked',flush=True); time.sleep(10)\n"
            holder = subprocess.Popen([sys.executable, "-c", holder_script, created["campaign"]], stdout=subprocess.PIPE, text=True)
            self.assertEqual("locked", holder.stdout.readline().strip())
            with self.assertRaisesRegex(RuntimeError, "locked"):
                review.next_packet(created["campaign"])
            holder.terminate(); holder.wait(); holder.stdout.close(); self.assertIsNotNone(holder.returncode)
            script = "from repo_graph.review import _locked; from pathlib import Path; import os,sys\nwith _locked(Path(sys.argv[1])): os._exit(0)\n"
            crashed = subprocess.run([sys.executable, "-c", script, created["campaign"]], capture_output=True, text=True)
            self.assertEqual(0, crashed.returncode, crashed.stderr)
            assigned = review.next_packet(created["campaign"])
            self.assertEqual("assigned", assigned["attempt"]["state"])
