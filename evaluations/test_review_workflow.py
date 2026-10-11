"""Minimal source-only regression checks for review-workflow accounting."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("review_workflow", Path(__file__).with_name("review_workflow.py"))
WORKFLOW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKFLOW)


def receipt(task_id, condition, reviewer_id):
    key = WORKFLOW.load_key()
    return {"pair_id": task_id, "condition": condition, "task_id": task_id,
            "source_binding": {"corpus_key_sha256": key["key_sha256"], "fixture_sha256": key["fixture_sha256"]},
            "reviewer": {"identity": reviewer_id, "model": "fixture-model", "reasoning": "medium", "codex_surface": "fixture-cli"},
            "workflow_calls": [
                {"id": "coordinator", "role": "coordinator", "status": "completed", "execution": "deterministic_no_model", "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}},
                {"id": "reviewer", "role": "reviewer", "status": "completed", "execution": "model", "usage": {"input_tokens": 20, "output_tokens": 10, "reasoning_tokens": 7}},
                {"id": "grader", "role": "grader", "status": "completed", "execution": "model", "usage": {"input_tokens": 3, "output_tokens": 2, "reasoning_tokens": 1}}],
            "independent_grader": {"identity": "grader", "provenance": {"method": "fixture"}, "call_id": "grader"},
            "outcome": {"task_success": True, "critical_misses": 0, "assertion_map": {"keyed": 1, "accounted": 1}}}


class ReviewWorkflowTest(unittest.TestCase):
    def test_partial_coverage_is_incomplete_and_reasoning_is_not_billed_twice(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "receipts.json"
            path.write_text(json.dumps({"schema_version": 1, "runs": [
                receipt("RW-CLI-DISCOVERY", "baseline", "baseline-session"),
                receipt("RW-CLI-DISCOVERY", "packet", "packet-session")]}))
            report = WORKFLOW.summarize_receipts(path)
        self.assertEqual(report["decision"], "incomplete")
        self.assertEqual(report["gates"]["zero_critical_miss"], "incomplete")
        self.assertEqual(report["pairs"][0]["baseline"]["workflow_usage"]["billed_tokens"], 35)
        self.assertEqual(report["pairs"][0]["baseline"]["workflow_usage"]["reasoning_tokens"], 8)
        failed = receipt("RW-CLI-DISCOVERY", "baseline", "failed-session")
        failed["workflow_calls"][1]["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "completed reviewer"):
            WORKFLOW._run(failed, WORKFLOW.load_key())
        failed["workflow_calls"][1]["status"] = "completed"
        failed["workflow_calls"][0]["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "completed coordinator"):
            WORKFLOW._run(failed, WORKFLOW.load_key())


if __name__ == "__main__":
    unittest.main()
