# ADR 0007: Bounded Repo Graph reviews in Codex

- Status: Accepted direction; implementation experimental, qualification pending
- Date: 2026-10-10
- Deciders: Repo Graph maintainers and the operator
- Scope decision: operator selected Repo Graph extension focused on Codex;
  execution of the saved read-only, quality-first plan approved
- Design: [Review workflow](../review-workflow-design.md)
- Execution: [Work packets and qualification](../review-workflow-execution.md)

## Context

Large test suites contain repeated setup, distinct failure contracts and
cross-file dependencies. Loading all source into one agent does not establish
complete or reliable review. Smaller workers need coherent source units,
durable progress and independent checking. The operator selected Codex as the
focus for expanding Repo Graph's use cases. A fresh Codex session should continue
the review without reconstructing it from a long conversation.

Repo Graph already owns local source discovery, bounded evidence and shared
Pi/Codex/Claude packaging. Its merged structural analysis is experimental and
has separate qualification gates. Anvil already owns work state when configured.

## Decision

Add a bounded review workflow to the canonical Repo Graph product. Start with
one local CLI implementation and the shared skill. Prepare immutable source
packets, record source-bound review results, and report remaining gaps. Use
Codex native subagents for automatic workers only after capability tests on the
actual calling surface. Do not add new Claude/Pi review adapters or a generic
adapter framework. Existing map/search installation contracts remain in force.

Separate packet delivery, reviewer claims, deterministic validation and
independent acceptance. Preserve exact source hashes, ranges, omissions and
uncertainty. Treat context-size profiles as calibration hypotheses; enforce
only the token, byte, request or spend limits the measured interface supports.

The planned first release runs read-only test-review campaigns in Codex. Optional
Anvil integration is deferred; unsupported Anvil-bound requests are refused.
Anvil remains authoritative for tasks, claims and acceptance where project rules
require it. Standalone records cannot impersonate Anvil acceptance. Read-only
automation requires exact-surface qualification; quality-first optimization is
the execution default. The packet alpha reports materialized-source completion
and incomplete dependency closure explicitly.

Test consolidation is the initial qualification task. Change-impact review,
dead-code investigation and journey understanding are later candidates that can
reuse evidence records after separate acceptance checks. Missing static edges
are never proof that code is dead.

## Options considered

| Option | Complexity | Portability | Tradeoff |
| --- | --- | --- | --- |
| One large prompt and shared transcript | Low initial setup | Broad | Weak progress attribution, repeated context and difficult review of omissions |
| Independent implementation in each harness | Medium initially, high maintenance | Behavior diverges | Native convenience at the cost of duplicated contracts and evaluators |
| MCP-first orchestration service | High for this slice | Shared tools, not shared lifecycle controls | Does not solve native session permissions, worker spawning or total-context visibility by itself |
| Shared CLI with Codex-scoped skill workflow | Low initial core, one integration | Versioned evidence can be reused without promising other harnesses | Exact Codex surface still needs lifecycle and usage qualification; selected direction |

## Consequences

Source packets and result schemas can be reused without adopting another task
service. Model/provider settings remain with Codex. Unsupported automatic
capabilities remain visible and leave the manual packet workflow available.

Full-file evidence requires new bounded acquisition; existing search synopses
and captured excerpts are insufficient for a whole-file review claim. Dynamic
dependencies remain unknown where static evidence cannot resolve them.

The workflow cannot guarantee model comprehension or universally optimal chunk
sizes. Independent assertion maps, fixed-task comparisons and retained failures
are required. Existing release qualification is not bypassed by adding a review
feature, and no new server, model backend or UI is required for the first slice.

## Action items

1. Resolve remaining automation and optimization defaults and record deferred scope.
2. Freeze source/result contracts and the actual Codex surface's capabilities.
3. Implement and qualify the installed Codex packet/resume journey before dispatch.
4. Qualify Codex native workers; keep other harness review work outside this plan.
5. Run the controlled test-review pilot and record per-case quality and cost.
6. Complete inherited and feature-specific release gates before consumer rollout.
