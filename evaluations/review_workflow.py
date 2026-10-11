#!/usr/bin/env python3
"""Account for frozen, source-only review-workflow comparisons.

Run "python evaluations/review_workflow.py --validate-corpus" before review.
After independent grading, run "python evaluations/review_workflow.py
--summarize RECEIPTS.json". A receipt records every coordinator, reviewer,
follow-up, failed, and grader call in workflow_calls. Billed-token deltas are
input plus output only; reported reasoning tokens remain separate because they
can be included in output tokens. This program never executes fixtures, calls
a model, or grades recommendations.
"""

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEY = ROOT / "evaluations/review_workflow_cases.json"
MAX_INPUT_BYTES = 1024 * 1024
TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens")
BILLED_FIELDS = TOKEN_FIELDS[:2]
CALL_ROLES = {"coordinator", "reviewer", "followup", "grader"}
CALL_EXECUTIONS = {"model", "deterministic_no_model"}


def _read_bounded(path):
    with path.open("rb") as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds the 1 MiB evidence limit")
    return raw


def _json(path):
    raw = _read_bounded(path)

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def invalid_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                       parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _count(value):
    return type(value) is int and value >= 0


def _relative_file(value):
    if not _nonempty(value):
        raise ValueError("source path must be a nonempty relative path")
    path = Path(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError("source path must be portable and relative")
    candidate = (ROOT / path).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError:
        raise ValueError("source path escapes the repository root") from None
    return candidate


def _usage(value, label):
    if not isinstance(value, dict) or set(value) != set(TOKEN_FIELDS):
        raise ValueError(f"{label} must name every token field")
    for field, count in value.items():
        if count != "unknown" and not _count(count):
            raise ValueError(f"{label}.{field} must be a nonnegative count or unknown")
    return value


def _usage_total(usages):
    totals = {field: 0 for field in TOKEN_FIELDS}
    for usage in usages:
        for field, count in usage.items():
            if count == "unknown":
                totals[field] = "unknown"
            elif totals[field] != "unknown":
                totals[field] += count
    billed = "unknown" if any(totals[field] == "unknown" for field in BILLED_FIELDS) else sum(
        totals[field] for field in BILLED_FIELDS)
    return {"billed_tokens": billed, "reasoning_tokens": totals["reasoning_tokens"],
            "by_field": totals}


def load_key(path=DEFAULT_KEY):
    """Read the frozen assertion key and validate its bounded source references."""
    key, key_sha256 = _json(path)
    if key.get("schema_version") != 1 or key.get("kind") != "review_workflow_source_key":
        raise ValueError("unsupported review-workflow source key")
    corpus_root = _relative_file(key.get("corpus_root"))
    cases = key.get("cases")
    if not isinstance(cases, list) or len(cases) != 12:
        raise ValueError("source key must contain exactly twelve task families")
    by_id = {}
    for case in cases:
        if not isinstance(case, dict) or not _nonempty(case.get("id")) or case["id"] in by_id:
            raise ValueError("source key has invalid or duplicate case IDs")
        if case.get("split") not in ("calibration", "held_out") or not isinstance(case.get("anchors"), list):
            raise ValueError(f"invalid split or anchors for {case['id']}")
        if not isinstance(case.get("required_assertions"), list) or not case["required_assertions"]:
            raise ValueError(f"missing keyed assertions for {case['id']}")
        by_id[case["id"]] = case
    calibration = set(key.get("calibration_case_ids", []))
    held_out = set(key.get("held_out_case_ids", []))
    if calibration & held_out or calibration | held_out != set(by_id):
        raise ValueError("source-key splits must be disjoint and complete")
    source_files = key.get("source_files")
    if not isinstance(source_files, dict) or not source_files:
        raise ValueError("source key has no fixture inventory")
    fixture_sha256 = {}
    for name, source in source_files.items():
        if (not isinstance(source, dict) or set(source) != {"path", "sha256"}
                or not _nonempty(source.get("sha256"))):
            raise ValueError(f"invalid source identity for {name}")
        fixture = _relative_file(source["path"])
        raw = _read_bounded(fixture)
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError(f"fixture digest mismatch: {source['path']}")
        fixture_sha256[name] = source["sha256"]
    for case in cases:
        for anchor in case["anchors"]:
            try:
                filename, line, symbol = anchor.split(":", 2)
                if not filename or "/" in filename or not line.isdecimal() or int(line) <= 0:
                    raise ValueError
                source = (corpus_root / filename).resolve()
                source.relative_to(corpus_root)
                content = _read_bounded(source).decode("utf-8").splitlines()
                if symbol not in content[int(line) - 1]:
                    raise ValueError
            except (IndexError, OSError, UnicodeError, ValueError):
                raise ValueError(f"invalid source anchor: {anchor}") from None
    return {"key": key, "key_sha256": key_sha256, "cases": by_id,
            "fixture_sha256": fixture_sha256}


def validate_corpus(path=DEFAULT_KEY):
    key = load_key(path)
    splits = {"calibration": 0, "held_out": 0}
    assertions = 0
    for case in key["cases"].values():
        splits[case["split"]] += 1
        assertions += len(case["required_assertions"])
    return {"status": "valid", "task_families": len(key["cases"]), "splits": splits,
            "keyed_assertions": assertions, "source_key_sha256": key["key_sha256"],
            "fixture_sha256": key["fixture_sha256"], "execution": "source_only_not_executed"}


def _run(record, key):
    required = {"pair_id", "condition", "task_id", "source_binding", "reviewer",
                "workflow_calls", "independent_grader", "outcome"}
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError("each run must contain the complete receipt contract")
    if not _nonempty(record["pair_id"]) or record["condition"] not in ("baseline", "packet"):
        raise ValueError("run has invalid pair ID or condition")
    case = key["cases"].get(record["task_id"])
    if case is None:
        raise ValueError("run references a task outside the frozen source key")
    binding = record["source_binding"]
    if not isinstance(binding, dict) or set(binding) != {"corpus_key_sha256", "fixture_sha256"}:
        raise ValueError("run must bind the corpus key and fixture identities")
    if binding["corpus_key_sha256"] != key["key_sha256"] or binding["fixture_sha256"] != key["fixture_sha256"]:
        raise ValueError("run source binding does not match the frozen corpus")
    reviewer = record["reviewer"]
    if not isinstance(reviewer, dict) or set(reviewer) != {"identity", "model", "reasoning", "codex_surface"}:
        raise ValueError("reviewer must identify identity, model, reasoning, and Codex surface")
    if not all(_nonempty(value) for value in reviewer.values()):
        raise ValueError("reviewer identity fields must be nonempty")
    calls = record["workflow_calls"]
    if not isinstance(calls, list) or not calls or len(calls) > 256:
        raise ValueError("run must retain a bounded nonempty workflow-call list")
    call_ids, call_roles, call_statuses, call_usage, failures, roles = set(), {}, {}, [], 0, set()
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"id", "role", "status", "execution", "usage"}:
            raise ValueError("workflow call must identify ID, role, status, execution, and observed-or-unknown usage")
        if (not _nonempty(call["id"]) or call["id"] in call_ids or call["role"] not in CALL_ROLES
                or call["execution"] not in CALL_EXECUTIONS or not _nonempty(call["status"])):
            raise ValueError("workflow calls need unique IDs, declared roles, and statuses")
        usage = _usage(call["usage"], "workflow-call usage")
        if call["execution"] == "deterministic_no_model" and (call["role"] != "coordinator"
                or any(value != 0 for value in usage.values())):
            raise ValueError("a no-model call must be a zero-usage deterministic coordinator")
        call_ids.add(call["id"])
        call_roles[call["id"]] = call["role"]
        call_statuses[call["id"]] = call["status"]
        roles.add(call["role"])
        failures += call["status"] != "completed"
        call_usage.append(usage)
    if not {"coordinator", "reviewer", "grader"} <= roles:
        raise ValueError("workflow calls must retain coordinator, reviewer, and grader usage")
    if not any(call_roles[call_id] == "coordinator" and call_statuses[call_id] == "completed" for call_id in call_ids):
        raise ValueError("a quality outcome requires a completed coordinator call")
    if not any(call_roles[call_id] == "reviewer" and call_statuses[call_id] == "completed" for call_id in call_ids):
        raise ValueError("a quality outcome requires a completed reviewer call")
    grader = record["independent_grader"]
    if not isinstance(grader, dict) or set(grader) != {"identity", "provenance", "call_id"}:
        raise ValueError("independent grader must identify provenance and its workflow call")
    if (not _nonempty(grader["identity"]) or grader["identity"] == reviewer["identity"]
            or call_roles.get(grader["call_id"]) != "grader" or call_statuses.get(grader["call_id"]) != "completed"):
        raise ValueError("self-grading identity or completed independent grader call is required")
    if not isinstance(grader["provenance"], dict) or not grader["provenance"]:
        raise ValueError("independent grader provenance is required")
    outcome = record["outcome"]
    if not isinstance(outcome, dict) or set(outcome) != {"task_success", "critical_misses", "assertion_map"}:
        raise ValueError("outcome must contain success, critical misses, and assertion-map counts")
    assertion_map = outcome["assertion_map"]
    expected = len(case["required_assertions"])
    if (type(outcome["task_success"]) is not bool or not _count(outcome["critical_misses"])
            or not isinstance(assertion_map, dict) or set(assertion_map) != {"keyed", "accounted"}
            or assertion_map.get("keyed") != expected or not _count(assertion_map.get("accounted"))
            or assertion_map["accounted"] > expected):
        raise ValueError("outcome does not account for the frozen assertion key")
    return {"pair_id": record["pair_id"], "condition": record["condition"], "task_id": record["task_id"],
            "source_binding": binding, "reviewer": reviewer, "workflow_call_count": len(calls),
            "failed_call_count": failures, "workflow_usage": _usage_total(call_usage),
            "workflow_call_statuses": {call_id: {"role": call_roles[call_id], "status": call_statuses[call_id]}
                                       for call_id in sorted(call_ids)},
            "independent_grader": grader, "outcome": outcome}


def _gate(condition, complete):
    return "passed" if complete and condition else "failed" if not condition else "incomplete"


def summarize_receipts(path, key_path=DEFAULT_KEY):
    key = load_key(key_path)
    receipts, _ = _json(path)
    if set(receipts) != {"schema_version", "runs"} or receipts["schema_version"] != 1:
        raise ValueError("receipts must be a schema-versioned object")
    if not isinstance(receipts["runs"], list) or not receipts["runs"]:
        raise ValueError("receipts must retain every baseline and packet run")
    pairs = {}
    for record in receipts["runs"]:
        run = _run(record, key)
        pair = pairs.setdefault(run["pair_id"], {})
        if run["condition"] in pair:
            raise ValueError("duplicate condition within a pair")
        pair[run["condition"]] = run
    results, reductions, task_ids = [], [], set()
    no_critical_miss = all_keyed = no_success_loss = True
    for pair_id, pair in sorted(pairs.items()):
        if set(pair) != {"baseline", "packet"}:
            raise ValueError(f"unmatched pair comparison: {pair_id}")
        baseline, packet = pair["baseline"], pair["packet"]
        if baseline["task_id"] in task_ids:
            raise ValueError("multiple comparisons for one frozen task are not a full-coverage receipt")
        task_ids.add(baseline["task_id"])
        for field in ("task_id", "source_binding"):
            if baseline[field] != packet[field]:
                raise ValueError(f"pair conditions differ on {field}: {pair_id}")
        for field in ("model", "reasoning", "codex_surface"):
            if baseline["reviewer"][field] != packet["reviewer"][field]:
                raise ValueError(f"pair settings differ on {field}: {pair_id}")
        no_critical_miss &= all(run["outcome"]["critical_misses"] == 0 for run in pair.values())
        all_keyed &= all(run["outcome"]["assertion_map"]["accounted"] == run["outcome"]["assertion_map"]["keyed"]
                           for run in pair.values())
        no_success_loss &= not (baseline["outcome"]["task_success"] and not packet["outcome"]["task_success"])
        before, after = baseline["workflow_usage"]["billed_tokens"], packet["workflow_usage"]["billed_tokens"]
        reduction = None
        if isinstance(before, int) and isinstance(after, int) and before > 0:
            reduction = (before - after) / before
            reductions.append(reduction)
        results.append({"pair_id": pair_id, "task_id": baseline["task_id"], "source_binding": baseline["source_binding"],
                        "baseline": baseline, "packet": packet,
                        "billed_token_reduction": reduction if reduction is not None else "unknown"})
    expected_ids = set(key["cases"])
    split_counts = {"calibration": 0, "held_out": 0}
    for task_id in task_ids:
        split_counts[key["cases"][task_id]["split"]] += 1
    complete = task_ids == expected_ids and split_counts == {"calibration": 5, "held_out": 7}
    all_comparable = complete and len(reductions) == len(results)
    median = statistics.median(reductions) if all_comparable else None
    token_condition = median is not None and median >= 0.25
    gates = {"zero_critical_miss": _gate(no_critical_miss, complete),
             "all_keyed_assertions_accounted": _gate(all_keyed, complete),
             "no_task_success_loss": _gate(no_success_loss, complete),
             "proposed_25_percent_median_billed_token_reduction": _gate(token_condition, complete) if median is not None else "incomplete" if not complete else "unknown"}
    decision = "passed" if complete and all(value == "passed" for value in gates.values()) else "failed" if "failed" in gates.values() else "incomplete"
    return {"status": "summarized", "source_key_sha256": key["key_sha256"], "pairs": results,
            "coverage": {"status": "complete" if complete else "incomplete", "task_families": len(task_ids),
                         "expected_task_families": len(expected_ids), "splits": split_counts,
                         "expected_splits": {"calibration": 5, "held_out": 7},
                         "missing_task_ids": sorted(expected_ids - task_ids)},
            "comparable_usage_pair_count": len(reductions) if complete else 0,
            "median_billed_token_reduction": median if median is not None else "unknown",
            "gates": gates, "decision": decision,
            "limits": ["Billed tokens are observed input plus output across every workflow call; estimates and dollars are ignored.",
                       "Reasoning tokens are retained separately and are never added to billed tokens.",
                       "No live model pilot or operator run ceiling is recorded by this source-only evaluator.",
                       "This tool accounts for independent grades; it does not generate or grade recommendations."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--validate-corpus", action="store_true", help="verify frozen source identities and anchors")
    actions.add_argument("--summarize", type=Path, metavar="RECEIPTS.json", help="summarize paired independent-review receipts")
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY, help="frozen source key (default: evaluations/review_workflow_cases.json)")
    args = parser.parse_args(argv)
    try:
        result = validate_corpus(args.key) if args.validate_corpus else summarize_receipts(args.summarize, args.key)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
