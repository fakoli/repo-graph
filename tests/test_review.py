import json
from pathlib import Path
import tempfile
import unittest

from repo_graph import review


class ReviewTests(unittest.TestCase):
    def make_repo(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / "source"; root.mkdir()
        (root / "tests").mkdir(); (root / "pkg").mkdir()
        (root / "tests" / "conftest.py").write_text("def fixture(): return 1\n")
        (root / "pkg" / "helper.py").write_text("def value(): return 1\n")
        (root / "tests" / "test_one.py").write_text("from pkg.helper import value\ndef test_value():\n    assert value() == 1\n")
        return temp, root

    def result_for(self, assigned, *, outcome="completed"):
        packet, attempt = assigned["packet"], assigned["attempt"]
        ranges = []
        for source in packet["sources"]:
            range_ = {key: source[key] for key in ("path", "sha256")}
            range_.update(start_line=1, end_line=source["end_line"]); ranges.append(range_)
        return {"packet_id": packet["packet_id"], "attempt_id": attempt["attempt_id"], "worker_id": attempt["worker_id"],
                "reviewed_ranges": ranges, "findings": [{"summary": "keeps distinct behavior", "citations": [ranges[0]]}],
                "assertion_map": [{"original": "tests/test_one.py:3", "disposition": "preserved", "evidence": "assertion retained"}], "outcome": outcome}

    def test_journey_resume_and_independent_acceptance(self):
        temp, root = self.make_repo()
        with temp:
            output = Path(temp.name) / "cache"
            created = review.plan(root, output=output)
            resumed = review.plan(root, output=output)
            self.assertTrue(resumed["resumed"])
            assigned = review.next_packet(created["campaign"], worker="codex-1")
            self.assertIn("worker_result", assigned["result_schema"])
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
            packet_file = next(Path(created["campaign"]).glob("packet-*.json"))
            original = packet_file.read_bytes()
            altered = json.loads(original)
            altered["source_text"][0]["text"] += "# tampered\n"
            packet_file.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "payload identity"):
                review.next_packet(created["campaign"])
            altered = json.loads(original)
            altered["state"] = "assigned"
            packet_file.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "publication is uncertain"):
                review.next_packet(created["campaign"])
            packet_file.write_bytes(original)
            assigned = review.next_packet(created["campaign"])
            result = self.result_for(assigned)
            with self.assertRaisesRegex(ValueError, "every materialized"):
                review.record(created["campaign"], dict(result, reviewed_ranges=result["reviewed_ranges"][:1]))
            with self.assertRaisesRegex(ValueError, "outcome"):
                review.record(created["campaign"], dict(result, outcome=[]))
            self.assertEqual("validated", review.record(created["campaign"], result)["state"])

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
