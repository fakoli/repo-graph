# Repo Graph Codex review execution plan

Status: execution authorized and active. Date: 2026-10-10.
Tracking: active Codex goal, Anvil PRD `codex-review`, coordinator bundle
`codex-review-implementation`. The operator explicitly authorized executing this
saved plan. Read-only recommendations and quality-first defaults govern this slice.
Design: [bounded review workflow](review-workflow-design.md).
Decision: [ADR 0007](adr/0007-bounded-review-workflow.md).

## Verified starting point

Planning uses refreshed main commit
`949db2f35a667ac877faebe81b887775e7937038`. The earlier development checkout was
143 commits behind and had unrelated design edits, so this plan lives in an
isolated documentation worktree. No earlier files were reconciled or reset.

The exact canonical project's Anvil status is initialized: existing
`code-understanding` work has 69 tasks, 33 done, 36 ready and zero active claims
at inspection. These are historical project tasks, not tasks for this proposal.
Resolve dependencies and existing ownership before adding or activating any
work; do not initialize another project or restart completed bundles.

Observed local command versions: Codex CLI 0.153.4 and Anvil 0.6.14 development
build/schema 22. These establish available commands, not worker qualification.
The Codex desktop surface may differ from
its installed CLI. Pin each actual tested surface in the eventual receipts.

Inspection found reusable inventory, guarded source I/O, structural queries,
captured evidence, shared skill and native packaging. No existing review packet
or campaign interface was found in the inspected main source. Deep analysis
and native analysis installation still have outstanding release qualification.
Shared imported-memory access was unavailable; planning uses source, project
status and local historical pointers without claiming shared retention.

## Scope and decision checkpoint

The operator selected extending Repo Graph specifically for Codex. The draft
keeps bounded read-only reviews and quality first as provisional defaults.
Initial feature scope is Python/pytest test consolidation in Codex. Optional
Anvil integration moves to follow-on work; no new Claude Code or Pi review
adapter belongs in this implementation. Existing installation behavior remains
covered by the product's regression gates.

Stage A delivers packet/record/status through the existing Codex skill. Stage B
enables native Codex workers after the exact calling surface passes its checks.
Stage C qualifies the measured pilot before a feature release. Subsequent
change-impact, dead-code and journey profiles require their own demonstrated
need and acceptance evidence. Automatic test edits remain a later scope.

## Work breakdown

The eight steps are grouped into four Anvil tasks: T001 covers R1–R3, T002
covers R4–R5, T003 covers R6, and T004 covers R7. R0's source, authority and
ownership decisions are recorded in this plan and PRD. Owners are
roles. Use one lead, at most two independent implementation workers and a
separate reviewer within the active concurrency limit. Assign disjoint files;
serialize changes to the CLI/shared skill. Every implementer reads changed
files completely and traces callers before editing.

| Packet | Owner and dependencies | Proposed files/seams | Deliverable and exit condition |
| --- | --- | --- | --- |
| R0: settle interfaces and inherited gates | Lead; remaining operator decisions | This design, ADR 0007, existing analysis/distribution tasks | Codex surface pinned, remaining defaults resolved, current release/base and actual inherited qualification dependencies recorded; no parallel project record |
| R1: source and packet contract | Focused worker; R0 | New `repo_graph/review.py`, existing `source.py` only if a demonstrated gap requires it; new `tests/test_review.py` | Deterministic packet manifest, full-file acquisition, explicit exclusions/omissions, content identity and finite bounds; no source execution |
| R2: bounded selection | Focused worker; R1 | `review.py`; existing `Queries`, inventory and function evidence consumed through supported seams | Complete test/fixture/implementation packets, deduplicated ranges, oversized-unit splitting and integration packets; no false complete-source claim |
| R3: records and CLI | Focused worker; R1, R2 | `review.py`, `cli.py`, `tests/test_review.py` | Plan/next/record/status, one coordinator writer, idempotent records, conflict/staleness detection and bounded status output |
| R4: Codex skill and installation | Focused worker; R3 | Existing `skills/repo-graph/SKILL.md`, README, `.codex-plugin/plugin.json` only if needed; existing `tests/harness_smoke.py` | Codex packet/record/status and fresh-session resume from the installed package; isolated install/repeat/upgrade/rollback checks; new review instructions are Codex-scoped |
| R5: Codex native delegation | Lead integration plus focused worker; R3, R4 | Codex section of the shared skill and focused integration checks; runtime glue only for demonstrated lifecycle needs | Fresh bounded read-only workers, authorized model choices, result capture, cancellation, usage knowledge and uncertain-outcome recovery on the pinned Codex surface |
| R6: comparative evaluation | Independent evaluation owner; R3, R5 | New `evaluations/review_workflow.py`, bounded public fixtures and results schema; private pilot evidence stays outside release assets | Frozen independent keys, task-matched Codex comparisons, all failures/usage retained, measured quality and efficiency decision |
| R7: release and consumers | Lead/release owner; R4–R6 and applicable inherited gates | README, release notes, versioned manifests and canonical consumer pins | Exact-source independent review, required CI, installed Codex workflow, existing product regressions, explicit limits and rollback; consumers follow the canonical release |

Start with one runtime module and one meaningful test file. Split modules only
when a real ownership or dependency boundary appears. Do not introduce a generic
adapter framework, scheduler, database service or embedding backend for R1–R4.
Use the installed skill-creator workflow when changing the existing skill, as
required by the contributor instructions.

R1 and R2 are sequential because selection relies on source identity. R4 follows
R3 and R5 follows the installed Codex packet journey. R6 corpus preparation can
start after R0, but measured comparisons wait for implementation. R7 depends on
the checks for the functionality actually shipped; do not claim experimental
graph analysis qualified through this feature. Record inherited gate relevance
in R0. Source merge alone is not release acceptance.

Follow-on work is outside R0–R7: optional Anvil evidence linking and additional
review profiles. Add no Anvil bridge, general adapter interface, extra harness
worker or future profile implementation in the first slice. If a project
requires Anvil authority, refuse an unsupported bound campaign rather than
silently replacing its task and acceptance rules with standalone records.

## Proposed operator experience

Installation continues through `repo-graph init --harness codex`. Project policy
loads automatically from validated conventional configuration. The user asks
`$repo-graph` to review a scope under the test-consolidation profile and budget.
The coordinator reports packet count, known gaps and available automation,
then dispatches within the previously authorized scope.

The operator sees progress such as: 12 packets planned, 4 validated results,
2 independently accepted, 1 needing more source and 5 pending. It also shows
reviewed source ranges, retained scenario counts and actual/unknown usage.
It never substitutes “all lines delivered” for a complete behavioral review.

Pause/resume and continuation in a fresh Codex session use the campaign ID and saved
evidence, not a pasted transcript. The first implementation supports same-host
continuation; cross-machine source rebinding is a separately tested extension.

## Offline checks: small number of complete journeys

Prefer focused scenario checks over one test for every JSON field. Keep
independent failure boundaries where combining them would conceal failures.

1. **Normal campaign:** plan a small pytest fixture, include implementation and
   cleanup, deliver a packet, record a cited result, independently accept it,
   and resume status in a fresh Codex session. Assert exact source identity,
   assertion destinations and no duplicate work.
2. **Incomplete evidence:** oversized file, unsupported syntax, missing fixture
   or unresolved caller remains visible; no full-review acceptance. A follow-up
   packet can close the gap without overwriting earlier observations.
3. **Source changes and races:** file mutation during read, edit after dispatch,
   ancestor symlink exchange, renamed/deleted dependency and mismatched snapshot
   all refuse or stale the correct result. Unknown impact invalidates a broader
   scope instead of assuming independence.
4. **Records and recovery:** duplicate result is idempotent, conflicting result
   is rejected, concurrent coordinator is refused, interrupted publication
   retains a recoverable prior state, and uncertain worker termination prevents
   duplicate dispatch.
5. **Budgets and privacy:** high fan-out, huge result, unknown tokenizer/usage,
   hidden context overhead, secret-bearing fixture and prompt injection retain
   precise bounded outcomes; no silent truncation or authority expansion.
6. **Authority boundary:** standalone mode is explicit; the first release refuses
   an Anvil-bound campaign because that integration is not implemented. The later
   integration must separately prove claim handling and uncertain-submission
   readback. It never writes the state database directly.

Add an independently checked integration case only where a primitive check
cannot establish the behavior. Reuse existing source-boundary and installation
fixtures; preserve their negative contracts. This plugin should demonstrate
the test discipline it is intended to help users adopt.

## Codex integration acceptance

Run the installed Codex workflow against a synthetic repository in an isolated home.
Provider-free smokes establish installation and command/result plumbing only.
A separately authorized bounded model run establishes worker behavior.

Require the selected Codex execution surface to show fresh context, source-write restriction,
explicit model selection/inheritance, completion and cancellation receipts,
owned-process/session cleanup, output truncation detection, and measured or
explicitly unknown usage. A failed capability leaves that surface at the manual
packet level; it does not weaken the automatic contract. Verify that resume
does not replay completed work or create a second coordinator for the campaign.

Use Codex native controls already exposed by the calling surface. CLI/app APIs
need exact-version probes before selecting any runtime glue. A source-only
packet path must remain useful without delegation. Do not translate missing
support into a new provider client or commands that overwrite a user's model,
provider or permission settings. Existing map/search checks for other harnesses
remain regression checks, not a requirement to add review support to them.

## Calibration and comparative pilot

Use the Anvil Serving suite as the private operator pilot, with a small public
synthetic corpus for reproducible regression checks. Pin exact source and test
configuration. The earlier suite measurement is orientation only: 11,203 passed,
73 skipped, 79.11% combined coverage and 72.42% branch coverage at source
`6a0956f717a066409d7b3b89d143c015b2a1d8e6`. Refresh if that source or environment
changes before implementation validation. No new test reduction is measured here.

1. Freeze 12 task families with an independent source-backed assertion key:
   CLI discovery, CLI refusal, HTTP fixture teardown, TTS transport, voice pool,
   REST/MCP parity, usage attribution, authentication, cancellation/drain,
   durable recovery, filesystem containment and no-op/non-candidate controls.
   Keep calibration families separate from held-out acceptance families.
2. Reviewers who create the source key do not grade their own generated
   recommendations. Record model/human provenance and blind the grading to
   packet strategy where practical. AI source review is not human UX evidence.
3. Compare ordinary scoped source exploration against packets on the same tasks,
   model, reasoning setting, Codex surface/version and source access. Both conditions
   may request missing source. Include all follow-up reads and lead review costs.
4. For context-size calibration, repeat fixed tasks with 10k, 20k and 40k total
   source-material variants. Hold the core task and required evidence constant;
   add controlled surrounding context instead of making larger tasks harder.
   Vary evidence placement. Report this separately from the workflow comparison.
5. Bound the initial paid experiment by an operator-selected ceiling. Begin with
   a small calibration subset before the full matrix. Use order-balanced runs,
   then repeat only to resolve variability or a disputed result; retain failures.
   Twelve paired families require at least 24 worker sessions and 12 independent
   pair-grading sessions, plus native qualification and any failed attempts.
   An eight- or 24-session ceiling therefore covers calibration only; it cannot
   produce a complete comparison decision.
6. Measure critical-behavior misses, unsafe removal recommendations, valid
   citation rate, assertion-map completeness, independent-review acceptance,
   input/output/reasoning tokens when available, dollars, wall time and tool calls.

Candidate acceptance criteria, to freeze in R0: zero accepted recommendations
that remove a keyed security/recovery behavior; all required assertion-map
entries accounted for; no worse task success than the scoped baseline; and the
existing ADR 0006 target of at least 25% median token reduction where comparable
usage is observable. These are pilot decision rules, not statistical guarantees.
Report denominators, paired task differences and uncertainty. A small successful
pilot does not establish a universal optimal context window.

If quality improves but tokens do not, keep the feature experimental and report
that tradeoff. If usage is unavailable, do not claim the token target passed.
If either strategy fails a critical behavior, repair the workflow and rerun
affected cases before expanding. Do not select only successful packets for the
comparison or hide reviewer/model calls from cost totals.

The initial workflow only recommends changes. Any later accepted implementation
batch uses the target repository's runner, independent review and unchanged-scope
coverage gate; its source edits and validation are separate evidence.

## Delivery checkpoints and stop conditions

| Checkpoint | Required evidence | Stop or narrow when |
| --- | --- | --- |
| Design ready | Operator decisions, source seam map, inherited gate map | Scope or authority choices conflict |
| Packet alpha | Offline source/record contracts and installed Codex packet/resume journey | Full-file acquisition or honest gap accounting fails |
| Automatic pilot | Pinned Codex surface's complete lifecycle and bounded paid trial | Permissions, unknown termination or budget reservation cannot be controlled |
| Qualified feature | Independent Codex comparison, exact-source CI and native installation | Quality/efficiency or applicable inherited release gates remain unpassed |
| Consumer rollout | Canonical release, pinned consumer updates and installed readback | Cached/runtime versions disagree or rollback is unproven |

Retain a manifest/schema version and refuse incompatible newer records. A
rollback keeps readable evidence and restores the prior plugin through its
native manager; it must not replay worker requests or erase source history.
For implementation, run affected checks once; run the required complete product
and native gates at release, broadening only after changes or unresolved failures.

## Execution checkpoint

- R0: current main remains `949db2f35a667ac877faebe81b887775e7937038`.
  The new eight-requirement, four-task PRD was approved using the operator's
  authorization to execute this same plan. Existing 69 code-understanding tasks
  and their acceptance/release holds were preserved.
- First packet selection uses full bounded source and Python AST candidate
  imports, with runtime dependencies explicit. Experimental graph queries are
  not required; this feature cannot accept earlier graph-analysis gates.
- R1–R3: experimental packet alpha implemented. Independent source review
  permits the earlier source checkpoint after integrity, freshness, denominator
  and bounded-record fixes. Follow-up v2 source adds recursive local import
  candidates and whole top-level test-function fragments with full-file identity
  and uncovered-range gaps. Partial files stay blocked; runtime dependency
  completeness and class/integration splitting remain unqualified. Immutable
  packets and one atomic lifecycle manifest remove split publication, and
  descriptor locks release on coordinator exit. Assigned/uncertain workers are
  never replayed. Fresh independent review cleared the frozen v2 runtime for
  an experimental source checkpoint. Source descriptor caps count failed
  decoding; serialized admission includes source escaping, metadata and gaps.
  Known omissions have exact details when they fit, otherwise a bounded stop
  summary and explicit lower-bound/unknown knowledge. A follow-up fixes split
  packets to reuse the same bounded fixture/package/import closure as whole
  files. Admission counts the materialized fragment; dependency reads across
  fragments share the campaign ceiling, and packet limits stop acquisition.
  Uncovered same-file ranges and omitted dependencies stay explicit. Independent
  review caught an old-v2 resume collision; construction identity now binds
  exact packet semantics, fragment ranges, gaps and anchors. The actual prior
  builder/current-builder probe preserved every old campaign byte and resumed
  only the matching new construction. No full-plan completion is claimed.
- R4: existing Codex skill extended and validated. Isolated installed Codex
  packet/result/independent-disposition/resume checks passed with synthetic
  results, including repository-config export refusal and reduced result limits.
  Existing Pi and Claude install/map/search checks also passed without provider
  calls or changes to live installations. This is plumbing evidence only.
- R5: a provider-free Codex CLI 0.153.4 standalone `:read-only` sandbox probe
  permitted source reading and denied writing. The earlier legacy-config probe
  refused because it required a permission profile. Neither probe qualifies
  model completion, fresh worker context, cancellation, usage or desktop workers.
- R6: a source-only public corpus and SHA256/line-anchored key are frozen for
  12 task families, split into five calibration and seven held-out families.
  Fixture integrity passed; no recommendations have been graded.
- The source-only evaluator requires all 12 matched families for a full pass,
  sums observed input/output across coordinator, reviewer, follow-up, failed and
  grader calls, and keeps reasoning separate. One regression journey verifies
  partial comparisons remain incomplete and failed calls cannot supply a
  completed quality result.
- Product Python, Node and structural evaluation checks passed. The first
  browser attempt failed while reading a navigated-away response; the unchanged
  rerun passed with no browser errors. Both attempts remain recorded rather than
  relabeling the first as a pass. Both initial CI runs reproduced that race.
  Source-response JSON capture now starts immediately on receipt; focused and
  full browser checks passed after the repair with all assertions preserved.
  Both repaired-source CI runs passed at `cf459c8ae574204c0bbdb784c23ee29fd03c065f`.
  The subsequent v2 source passed 127 product tests, 203 structural checks,
  eight evaluation contract checks, corpus integrity, isolated Codex/Claude/Pi
  smokes and wheel construction. Both CI runs at `09eefe6` passed those gates
  but reproduced the browser response-body race despite immediate JSON capture.
  The browser check now buffers the actual server API response through the
  existing route-fetch pattern before delivering it to the browser. It also
  checks rendered text against the captured response, preserves byte-range and
  keyboard-focus assertions, and removes interception before witness requests.
  Independent review cleared this test repair for the experimental checkpoint;
  its new committed head still requires fresh CI. A focused invocation with
  system Python failed before browser startup because the optional parser was
  absent; the documented virtual-environment invocation passed.
  The full browser run then exposed the same body-retention boundary in the
  contract-impact response collector. Its temporary route now buffers the real
  API response, retains the prior Impact-operation filter and all scope/response
  assertions, and cleans up in `finally`. Independent review rejected an
  intermediate repair that lost that operation filter. The corrected full
  browser run passed with no browser errors; both failures and review correction
  remain retained.
  Receipts record checks at their source hashes; current PR checks are the
  authority for its latest committed head.
- R7: source checkpoint delivery is separate from feature release. Version pins
  and consumers remain unchanged while model/efficiency gates are unpassed.
  [V2 source review](../evaluations/results/review-workflow/source-review-v2.json) and
  [offline receipts](../evaluations/results/review-workflow/offline.json) retain
  the current qualification limits.
- A real source-only preparation under eight-packet/1-MiB limits initially
  produced zero packets, then one, before serialized admission was corrected.
  The final attempt retains two planned packets from 470 requested files;
  462 files are blocked by the packet limit and six by the read limit.
  All attempts remain recorded. This executed no target tests or model calls
  and establishes no review-quality, test-reduction or coverage result.
- The live-pilot run ceiling is pending an operator answer. Offline checks and
  implementation proceed. No live-comparison or token-efficiency result exists.
- [Native preparation](../evaluations/results/review-workflow/native-preparation.json)
  records observed ephemeral/JSON/schema/tool-disable CLI controls. Their live
  effects, provider readback, cancellation and usage remain unqualified. The
  no-network standalone sandbox probe is provider-free and cannot wrap a model
  invocation that needs provider transport.
- Shared imported-memory access remains unavailable in this tool connection.

## Remaining decisions that can optimize the design

Settle these in R0; Codex and Repo Graph ownership are already selected:

- Retain the proposed read-only automation depth and quality-first priority, or
  revise them explicitly. Optional Anvil integration is deferred in this draft.
- Pin the actual Codex working surface, supported fresh-worker controls and
  usage visibility. Do not require a second Codex surface without demonstrated need.
- The pilot's dollar/time ceiling and whether a strict cap is required.
- Whether review completion must cover every requested line or may be scoped
  to named behaviors; the report must distinguish those two contracts.
- Which independent review evidence is sufficient for a recommendation versus
  an actual code deletion. Human UX claims still require human evidence.

After the test-consolidation pilot, select one next use case from change-impact
review, dead-code investigation or journey understanding. Reuse packet evidence
and progress where they fit; freeze a task-specific result contract and independent
cases before advertising another profile. Keep other harnesses and cross-machine
transfer outside this plan unless the operator expands the scope.
