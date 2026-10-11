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
            (Path(created["campaign"]) / review.status(created["campaign"])["packets"][0]["result"]["path"]).unlink()
            stale = review.status(created["campaign"])["packets"][0]
            self.assertEqual("stale", stale["state"])
            self.assertEqual("evidence_invalid", stale["stale_reason"])
            self.assertEqual("stale", review.status(created["campaign"])["packets"][0]["state"])
            # Explicit members form one atomic, source-only integration packet.
            # Shared closure is materialized once and both test files retain
            # their independent original assertion identities.
            (root / "tests" / "test_queue_extra.py").write_text(
                "from pkg.helper import value\ndef test_queue_extra():\n    assert value() == 1\n")
            grouped = review.plan(root, scope="tests/test_one.py", members=["tests/test_queue_extra.py"], output=Path(temp.name) / "group-cache")
            self.assertEqual({"requested": 2, "eligible": 2, "blocked": 0, "packets": 1},
                             {key: grouped["counts"][key] for key in ("requested", "eligible", "blocked", "packets")})
            self.assertTrue(review.plan(root, scope="tests/test_one.py", members=["tests/test_queue_extra.py"], output=Path(temp.name) / "group-cache")["resumed"])
            grouped_assigned = review.next_packet(grouped["campaign"], worker="codex-group")
            grouped_packet = grouped_assigned["packet"]
            self.assertEqual(["tests/test_one.py", "tests/test_queue_extra.py"], grouped_packet["test_members"])
            self.assertEqual({"tests/test_one.py", "tests/test_queue_extra.py", "tests/conftest.py", "pkg/__init__.py", "pkg/helper.py", "pkg/extra.py"},
                             {source["path"] for source in grouped_packet["sources"]})
            self.assertEqual(2, len(grouped_packet["anchors"]["assertions"]))
            incomplete = self.result_for(grouped_assigned)
            incomplete["assertion_map"].pop()
            with self.assertRaisesRegex(ValueError, "each original assertion"):
                review.record(grouped["campaign"], incomplete)
            grouped_result = self.result_for(grouped_assigned)
            grouped_saved = review.record(grouped["campaign"], grouped_result)
            grouped_decision = {"kind": "independent_decision", "packet_id": grouped_result["packet_id"], "attempt_id": grouped_result["attempt_id"],
                                "result_sha256": grouped_saved["sha256"], "reviewer_id": "astra-group", "disposition": "accepted",
                                "provenance": {"model": "astra", "surface": "codex", "reasoning": "high"}, "rationale": "independent group review"}
            self.assertEqual("accepted", review.record(grouped["campaign"], grouped_decision)["state"])
            self.assertEqual(["tests/test_one.py", "tests/test_queue_extra.py"], review.status(grouped["campaign"])["packets"][0]["test_members"])
            (root / "tests" / "test_queue_extra.py").write_text("changed = True\n" + (root / "tests" / "test_queue_extra.py").read_text())
            self.assertEqual("stale", review.status(grouped["campaign"])["packets"][0]["state"])
            (root / "tests" / "test_group_large.py").write_text("#" * 300)
            blocked_group = review.plan(root, scope="tests/test_one.py", members=["tests/test_group_large.py"], output=Path(temp.name) / "group-blocked", limits={"file_bytes": 128})
            self.assertEqual(0, blocked_group["counts"]["packets"])
            self.assertEqual(2, blocked_group["counts"]["blocked"])
            disappearing = root / "tests" / "test_group_disappears.py"
            disappearing.write_text("def test_disappears():\n    assert True\n")
            primary_bytes = (root / "tests" / "test_one.py").stat().st_size
            original_read, streamed = review.SourceRoot.read, [0]
            def disappear_after_info(owner, path, *args, **kwargs):
                measurements = kwargs.setdefault("measurements", {})
                if owner.root == root and path == "tests/test_group_disappears.py":
                    disappearing.unlink()
                try:
                    return original_read(owner, path, *args, **kwargs)
                finally:
                    if owner.root == root:
                        streamed[0] += measurements.get("stream_bytes", 0)
            with patch.object(review.SourceRoot, "read", disappear_after_info):
                raced_group = review.plan(root, scope="tests/test_one.py", members=["tests/test_group_disappears.py"], output=Path(temp.name) / "group-race")
            self.assertEqual(0, raced_group["counts"]["packets"])
            self.assertEqual(2, raced_group["counts"]["blocked"])
            self.assertEqual(primary_bytes, streamed[0])
            race_rows = review.status(raced_group["campaign"])["inventory_blocked"]
            self.assertTrue(all("group_blocked:tests/test_group_disappears.py:source_unavailable" in row["reason"] for row in race_rows))

    def test_packet_integrity_and_interrupted_publication_do_not_dispatch(self):
        temp, root = self.make_repo()
        with temp:
            created = review.plan(root, output=Path(temp.name) / "cache")
            manifest = Path(created["campaign"]) / "campaign.json"; manifest_original = manifest.read_bytes()
            for schema in ("repo-graph-review-v1", "repo-graph-review-v2", "repo-graph-review-v3"):
                legacy = json.loads(manifest_original); legacy["schema"] = schema; manifest.write_text(json.dumps(legacy))
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
            (Path(created["campaign"]) / review.status(created["campaign"])["packets"][0]["result"]["path"]).unlink()
            self.assertEqual("stale", review.record(created["campaign"], result)["state"])
            # The result blob is durable before the sole manifest publication. A
            # failed publication leaves the assigned manifest and permits only
            # idempotent receipt reconciliation, not a second dispatch.
            other = review.plan(root, output=Path(temp.name) / "retry-cache")
            pending = review.next_packet(other["campaign"]); pending_result = self.result_for(pending)
            with patch.object(review, "_save", side_effect=OSError("interrupted before manifest")):
                with self.assertRaises(OSError): review.record(other["campaign"], pending_result)
            self.assertIsNone(review.next_packet(other["campaign"])["packet"])
            artifacts = {path.name: path.read_bytes() for path in Path(other["campaign"]).glob("result-*.json")}
            with self.assertRaisesRegex(ValueError, "conflicting result"):
                review.record(other["campaign"], dict(pending_result, outcome="needs_source"))
            self.assertEqual(artifacts, {path.name: path.read_bytes() for path in Path(other["campaign"]).glob("result-*.json")})
            orphan = next(Path(other["campaign"]).glob("result-*.json"))
            orphan.write_text("{}")
            with self.assertRaises(ValueError):
                review.record(other["campaign"], pending_result)
            self.assertEqual("{}", orphan.read_text())
            orphan.write_bytes(artifacts[orphan.name])
            recovered = review.record(other["campaign"], pending_result)
            self.assertEqual("validated", recovered["state"])
            self.assertTrue(recovered["idempotent"])

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
            split_text = "from pkg.helper import value\nblob = '" + "x" * 3000 + "'\n\ndef tagged(fn): return fn\n\n@tagged\ndef test_left():\n    assert value() == 1\n\n# second context\ndef test_right():\n    assert value() == 1\n"
            (root / "tests" / "test_split.py").write_text(split_text)
            split = review.plan(root, scope="tests/test_split.py", output=Path(temp.name) / "split-cache", limits={"packet_bytes": 2400, "packets": 2})
            self.assertEqual(2, split["counts"]["packets"])
            assigned_packets = [review.next_packet(split["campaign"]) for _ in range(split["counts"]["packets"])]
            split_assigned = next(value for value in assigned_packets if value["packet"]["sources"][0].get("fragment"))
            first = split_assigned["packet"]
            self.assertIn("uncovered_source_unit", " ".join(first["gaps"]))
            self.assertIn("@tagged", first["source_text"][0]["text"])
            self.assertEqual(first["sources"][0]["start_line"], first["sources"][0]["fragment"]["start_line"])
            self.assertEqual({"tests/test_split.py", "tests/conftest.py", "pkg/__init__.py", "pkg/helper.py", "pkg/extra.py"}, {source["path"] for source in first["sources"]})
            self.assertFalse(first["dependencies_complete"])
            for packet_file in Path(split["campaign"]).glob("packet-*.json"):
                self.assertLessEqual(len(packet_file.read_bytes()), 2400)
            legacy_output = Path(temp.name) / "legacy-construction-cache"
            with patch.object(review, "PACKET_CONSTRUCTION", "split-closure-v0"):
                legacy = review.plan(root, scope="tests/test_split.py", output=legacy_output, limits={"packet_bytes": 2400, "packets": 2})
            legacy_bytes = {path.relative_to(legacy["campaign"]): path.read_bytes() for path in Path(legacy["campaign"]).glob("*.json")}
            rebuilt = review.plan(root, scope="tests/test_split.py", output=legacy_output, limits={"packet_bytes": 2400, "packets": 2})
            self.assertFalse(rebuilt["resumed"])
            self.assertNotEqual(legacy["campaign_id"], rebuilt["campaign_id"])
            self.assertEqual(legacy_bytes, {path.relative_to(legacy["campaign"]): path.read_bytes() for path in Path(legacy["campaign"]).glob("*.json")})
            self.assertTrue(review.plan(root, scope="tests/test_split.py", output=legacy_output, limits={"packet_bytes": 2400, "packets": 2})["resumed"])
            invalid = self.result_for(split_assigned); invalid["reviewed_ranges"][0]["start_line"] = 1
            with self.assertRaises(ValueError): review.record(split["campaign"], invalid)
            stored = review.record(split["campaign"], self.result_for(split_assigned))
            decision = {"kind": "independent_decision", "packet_id": first["packet_id"], "attempt_id": split_assigned["attempt"]["attempt_id"], "result_sha256": stored["sha256"], "reviewer_id": "independent", "disposition": "accepted", "provenance": {"model": "other", "surface": "codex", "reasoning": "high"}, "rationale": "range checked"}
            self.assertEqual("accepted", review.record(split["campaign"], decision)["state"])
            self.assertGreater(first["sources"][0]["start_line"], 1)
            (root / "tests" / "test_split.py").write_text("changed = True\n" + (root / "tests" / "test_split.py").read_text())
            self.assertIn("stale", [row["state"] for row in review.status(split["campaign"], limit=100)["packets"]])
            omitted = review.plan(root, scope="tests/test_split.py", output=Path(temp.name) / "split-omitted-cache", limits={"packet_bytes": 2400, "packets": 1, "source_bytes": len("changed = True\n" + split_text)})
            self.assertEqual(1, omitted["counts"]["packets"])
            omitted_packet = review.next_packet(omitted["campaign"])["packet"]
            self.assertFalse(omitted_packet["dependencies_complete"])
            self.assertIn("source_read_budget_dependency", " ".join(omitted_packet["gaps"]))
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
            for blank in ("", " \t"):
                incomplete = dict(result, assertion_map=[dict(row, evidence=blank) for row in result["assertion_map"]])
                with self.assertRaisesRegex(ValueError, "assertion map text"):
                    review.record(created["campaign"], incomplete)
                incomplete = dict(result, findings=[dict(result["findings"][0], summary=blank)])
                with self.assertRaisesRegex(ValueError, "invalid finding"):
                    review.record(created["campaign"], incomplete)
            saved = review.record(created["campaign"], result)
            with self.assertRaises(ValueError):
                review.record(created["campaign"], dict(result, outcome="needs_source"))
            decision = {"kind": "independent_decision", "packet_id": result["packet_id"], "attempt_id": result["attempt_id"],
                        "result_sha256": saved["sha256"], "reviewer_id": "other", "disposition": "accepted",
                        "provenance": {"model": "other", "surface": "codex", "reasoning": "high"}, "rationale": "independent source review"}
            for blank in ("", " \t\n"):
                with self.assertRaisesRegex(ValueError, "provenance and rationale required"):
                    review.record(created["campaign"], dict(decision, rationale=blank))
                for key in decision["provenance"]:
                    with self.assertRaisesRegex(ValueError, "provenance and rationale required"):
                        review.record(created["campaign"], dict(decision, provenance=dict(decision["provenance"], **{key: blank})))
                for key in ("packet_id", "attempt_id", "result_sha256", "reviewer_id"):
                    with self.assertRaisesRegex(ValueError, "identity fields required"):
                        review.record(created["campaign"], dict(decision, **{key: blank}))
                with self.assertRaisesRegex(ValueError, "worker identity required"):
                    review.next_packet(created["campaign"], worker=blank)
            with self.assertRaises(ValueError):
                review.record(created["campaign"], dict(decision, reviewer_id="same"))
            self.assertEqual("validated", review.status(created["campaign"])["packets"][0]["state"])
            # Earlier v4 validators could persist an accepted blank decision.
            # Refuse it on resume/read as well, preserving the saved bytes.
            manifest = Path(created["campaign"]) / "campaign.json"
            original = manifest.read_bytes()
            attempt = json.loads(original)["packets"][0]["attempts"][0]
            invalid = [{"decision": dict(decision, rationale=" \t\n")},
                       {"decision": dict(decision, provenance=dict(decision["provenance"], model=" \t"))},
                       *[{"decision": dict(decision, **{key: value})} for key, value in (
                           ("packet_id", "other-packet"), ("result_sha256", "0" * 64),
                           ("attempt_id", "other-attempt"), ("reviewer_id", "same"), ("disposition", "rejected"))],
                       {"result": None}, {"attempts": [dict(attempt, outcome="uncertain")]},
                       {"attempts": [dict(attempt, state="cancelled")]},
                       {"attempts": [attempt, attempt]}, {"state": "stale", "stale_reason": None}]
            for fields in invalid:
                historical = json.loads(original)
                historical["packets"][0].update(state="accepted", decision=decision)
                historical["packets"][0].update(fields)
                manifest.write_text(json.dumps(historical))
                historical_bytes = manifest.read_bytes()
                for observe in (lambda: review.status(created["campaign"]),
                                lambda: review.next_packet(created["campaign"]),
                                lambda: review.plan(root, output=Path(temp.name) / "cache"),
                                lambda: review.record(created["campaign"], decision)):
                    with self.assertRaises(ValueError):
                        observe()
                    self.assertEqual(historical_bytes, manifest.read_bytes())

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
