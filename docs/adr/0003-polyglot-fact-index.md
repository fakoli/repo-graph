# ADR 0003: Shared incremental polyglot fact index

- Status: Proposed
- Date: 2026-10-06

## Context

Repository structure and import heuristics cannot answer which functions might
call each other or explain a business workflow. Large monorepos also make repeated
parsing and loading whole graphs into an agent context expensive. A grammar's
language count does not establish accurate binding, type inference or call targets.

## Proposed decision

Keep one persistent index for inventory, syntax facts and resolved relationships.
Parse each changed file once per parser/rule version; cache its content hash and
source ranges. Run name/type/call resolution over stored facts, revisiting affected
dependents when definitions or rules change. Multiple analysis passes are allowed
without multiple independent repository scans.

Record files, definitions, references, imports and callsites as shared facts.
Candidate call targets carry their source evidence and resolution method. Keep
resolved, candidate and unresolved relationships distinguishable. Runtime-observed
relationships, if later imported, remain a separate evidence type. Preserve
unknowns for reflection, generated code and dynamic loading instead of inventing
an edge or treating absence as proof.

Language grammars and binding/type rules feed the same index. Framework rules
interpret facts for routes, jobs and ORM dispatch. Cross-language links need
evidence such as an API schema, RPC definition or explicit foreign-function
binding; matching names alone are not sufficient.

Human diagrams use a hierarchy with bounded expansion, component aggregation and
source inspection. Agent search returns bounded source-backed neighborhoods or
reachable slices. Whole-repository views and queries share that same index.

## Evaluation before implementation selection

Evaluate an existing compatible engine before writing a new resolver. Compare
MIT codebase-memory-mcp, reusable name-binding techniques and compiler index
imports against a frozen corpus; keep the existing scanner as a baseline. Do
not choose a graph database, rewrite language or embedding model without a
measured limitation that it solves.

Use Odoo for business workflows, Django for Python controls, and a synthetic
Python/TypeScript service with an explicit API contract for cross-language
boundaries. Measure per-language target precision/recall, unresolved rates,
incremental correctness, cold/warm time and peak memory. Evaluate agent retrieval
and human navigation separately. Report source revision, analyzer version,
hardware, exclusions and individual failures.

## Consequences

Polyglot discovery is feasible; equivalent semantic precision across every
language is not promised. Ship a coverage matrix for syntax, binding, candidate
calls and framework links. Possible static targets are not a complete list of
runtime executions or proof that a business rule runs.

This is a proposal. Version 0.6.0 consolidates packaging and harness support;
it does not implement this index or a function call graph. See the
[feasibility report](../polyglot-feasibility.md) for research and acceptance gates.
