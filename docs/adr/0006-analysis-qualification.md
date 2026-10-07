# ADR 0006: Qualify correctness, efficiency and navigation separately

- Status: Proposed
- Date: 2026-10-06
- Deciders: Repo Graph maintainers

## Context

Published work supports incremental facts, demand-driven resolution and
structural retrieval. Its corpora, language coverage and grading vary. A fast
graph query, grammar count or attractive screenshot cannot establish accurate
calls, token-efficient agent work or human understanding. Existing evaluations
cover file search and viewer regressions; they do not qualify function analysis.

## Proposed decision

Freeze fixture facts and held-out questions before comparing engines. Preserve
the current import/file-search baseline, a native-binding syntax baseline and
the selected reusable engine's individual failures. Report per-language and
per-construct results, with revision, rule/grammar versions, configuration,
exclusions, hardware class and source/index identity.

Use the pinned Odoo/Django revisions in
[the feasibility report](../polyglot-feasibility.md#evaluation-before-choosing-an-engine)
for business/framework and Python-control work. Retain AWS/Kubernetes for the
existing Go scale/regression baseline. A small Python/TypeScript/Go fixture
with explicit service contracts gives known cross-language positives and
negatives. Large fixtures remain outside release assets.

## Required gates

| Dimension | Gate and retained evidence |
|---|---|
| Source/index safety | No outside-root reads; explicit source-owned output; stale embedding writes rejected; synthetic ancestor-symlink, output-reuse and overlap checks |
| Engine lifecycle | Isolated configuration/cache, coexistence with existing sessions, cancellation and worker/daemon cleanup; no account-wide installer changes during evaluation |
| Syntax/direct binding | Exact expected fixture definitions, ranges and direct-call target sets; no falsely exact ambiguous/dynamic sites |
| Real call quality | Initial proposed target: precision >=95% and recall >=85% for reviewed supported direct/import-alias constructs, separately per language; dynamic/framework cases reported separately |
| Uncertainty/coverage | Every fixture unknown, excluded and partial file/site survives; account for all inventoried files; reference/call/observed evidence distinguishable |
| Incremental correctness | Canonicalized semantic facts equal clean rebuild after body/export/type/config/contract edits, additions resolving negative lookups, deletion/rename and cycles; exclude timestamps/local surrogate IDs from comparison |
| Scale | Cold/OS-warm scans, unchanged repeat, one-file/dependent updates, stage times, peak RSS, disk size and fact counts; measure scoped query p50/p95 and budget exhaustion |
| Query work | High-fan-out fixture respects work/output budgets and cancellation; counts remain honest; fixed-snapshot pagination has no skips/duplicates and rejects changed/expired generations |
| Native adoption | Proposed Rust threshold: >=20% end-to-end improvement on equivalent representative workloads over the best simpler option, without >10% warm-path or peak-memory regression; no trade of correctness for speed |
| Agent usefulness | Frozen tasks with identical model/harness/settings, order-balanced runs and independently reviewed answers; measure task success, evidence correctness, tokens, tool calls and latency; proposed target >=25% median token reduction with no task-success loss versus the strongest current baseline |
| Human usefulness | At least five independent reviewers perform entrypoint, possible-path, uncertainty and contract tasks in counterbalanced current/new views with the same source access; proposed target >=80% correct completion and zero false certainty conclusions; record time and mistakes, not only screenshots |
| Distribution/UX | Isolated wheel/Git-source Pi/Codex/Claude checks; missing optional backend; current keyboard, narrow-screen, source jump and browser-error regressions |

These numeric values are initial proposal targets, not results or statistical
guarantees. Report counts, denominators and uncertainty. Small task/reviewer sets
support a pilot decision, not a universal effectiveness claim. Set the large
corpus cold-time/RSS budget after measuring a reference baseline; do not invent
a million-file qualification from a synthetic vector scan. Warm scoped query
p95 should remain below the existing one-second budget, with cold startup
reported separately.

Experimental selection requires the component gates: source safety, lifecycle,
syntax/binding, call quality, uncertainty, incremental correctness, query work
and optional installation. Scale measurements identify trade-offs; the Rust
adoption threshold applies when proposing that acceleration. Agent, human and
complete distribution/UX gates qualify the later feature release. Passing an
engine experiment alone does not accept the user-facing feature.

## Options considered

| Option | Assessment |
|---|---|
| Adopt published headline numbers | Cheap but mismatched corpora/versions/grading; reject as qualification |
| One aggregate score | Hides language/construct failures and correctness/performance trade-offs; reject |
| Model grades its own summaries | Fast but not independent source verification; reject |
| Frozen component, agent and human checks | More setup, but identifies the benefit and its limits; preferred |

## Consequences

Engine selection can fail even when query latency is good. Accuracy can pass
while distribution or navigation fails. Keep those outcomes visible. Runtime
traces can cross-check a scenario but cannot enumerate every possible static
target. Jev may rank retrieved evidence; its judgment is not the answer key.

## Action items

1. [ ] Freeze task/fact manifests, supported constructs and source snapshots.
2. [ ] Extend existing evaluation commands with the selected engine comparison.
3. [ ] Run component and update checks before costly agent/human evaluation.
4. [ ] Retain per-case outcomes and resource receipts, including regressions.
5. [ ] Accept only the decisions whose actual gates passed; keep unqualified features Proposed.

## Approved source-review exception (2026-10-06)

For the first sixteen T004 real-call sites, the user authorizes an independent
Astra AI source reviewer. Freeze its judgments, corrections, unsupported cases,
model provenance and explicit assumptions before engine comparisons. Report
call-quality measurements as comparisons against an AI-reviewed source key.
This exception does not provide independent human UX or human agent-answer
evidence for T027, and models do not create runtime structural facts. The other
component, scale, distribution, acceptance and publication gates still apply.
