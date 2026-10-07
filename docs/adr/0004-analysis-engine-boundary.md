# ADR 0004: Reuse and qualify a native analysis engine before custom Rust

- Status: Proposed
- Date: 2026-10-06
- Deciders: Repo Graph maintainers

## Context

Function analysis needs syntax, binding/type rules and dependency invalidation.
Rewriting Python in Rust would not provide those semantics. Current source
extraction and search cataloguing read changed files separately. Pi, Codex and
Claude already share a Python entrypoint and native installation.

The [research comparison](../polyglot-feasibility.md#implementation-options-and-successful-precedents)
inspected reusable engines, compiler indexes and native bindings. No competing
engine or Rust core was benchmarked. Core mapping/keyword use remains dependency-free.

## Proposed decision

Compare a small Tree-sitter Python-binding syntax baseline with native engine
reuse on the same source/evidence workload. The pinned codebase-memory-mcp CLI
is not a selected backend. Its README describes daemon-backed one-shot commands,
but the exact pinned [local CLI implementation](https://github.com/DeusData/codebase-memory-mcp/blob/268a9d8886642eb7f9b2ce45f5ce27cdecf0f519/src/main.c#L2857-L3021)
runs a local server and returns before MCP daemon bootstrap. Its documented
`CBM_RUNTIME_DIR` scopes the rendezvous and associated locks; temporary HOME/cache
alone does not establish isolation. Preserve this source/documentation discrepancy.
Qualify actual coexistence, cancellation and cleanup before adoption; do not stop
existing sessions or run its installer. The screening below rejects this pin as
a complete evidence backend because its graph surface loses required callsite
ranges and unresolved sites.

Experimental engine selection requires the component gates in
[ADR 0006](0006-analysis-qualification.md): source safety, lifecycle, evidence,
uncertainty, incremental equivalence, bounded queries and optional setup.
Measure costs before selection; the native speed threshold applies to a custom
Rust acceleration decision. Agent and human gates follow the UX slice and are
required for feature acceptance, rather than for starting that experiment.

Keep Python orchestration, shared harness entrypoints and the existing viewer.
Use supported engine queries/export instead of copying frontends or coupling
to private database tables. If the chosen engine owns the structural store,
derived retrieval evidence may be cached without duplicating every graph fact.
Import existing SCIP artifacts as optional binding evidence; ordinary symbol
references become calls only with callsite syntax and documented resolution.

A batched Rust/PyO3 extension becomes a candidate when profiling identifies
extraction/traversal/resolution as the limiting cost and reuse cannot solve it.
Its boundary processes whole bounded batches and returns compact facts; it
detaches suitable work from Python and keeps database writes controlled.

## Options considered

| Option | Complexity / cost | Scale benefit | Main constraint |
|---|---|---|---|
| Existing Python + Tree-sitter bindings | Small syntax prototype; add grammar dependencies only to analysis setup | Native parsing; shared reads and selective queries | Binding/type semantics remain work; measure Python traversal/conversion |
| Reused native engine CLI | Adapter and dependency qualification | Existing resolution/incremental pipeline | Evidence and lifecycle contracts must pass; schema remains engine-owned |
| Custom Rust/PyO3 | Own analyzer plus native wheel matrix | Potential CPU parallelism and fewer Python objects | No measured gain; semantics and maintenance remain ours |
| Compiler/SCIP imports | Import/encoding/provenance checks | Stronger bindings using project tooling | Environment-dependent; not a complete call graph |
| Joern as default | JVM/frontends and graph integration | Richer CPG/call/dataflow analysis | Larger setup; use as a quality comparison before default adoption |

## Trade-off analysis

Reuse can deliver analysis sooner but adds a dependency whose heuristics must
remain visible. A native CLI has process/serialization costs; a binding has
ABI/wheel costs. Measure both end-to-end rather than selecting by implementation
language. Engine rejection leaves the current product usable and this choice
Proposed; it does not authorize a full rewrite automatically.

## Consequences

Native Git installation does not install grammar wheels or compile extensions.
Analysis installation must own setup, verify the selected binary/grammar
versions and report missing support clearly. A future extension should use a
separate optional module name so source-checkout execution does not shadow it.
Prebuilt wheel coverage includes supported OS/architectures, not just Python ABI.
Engine adoption must explicitly resolve the first slice's daemon exclusion;
CLI invocation is not proof of an independent process boundary.

## Action items

1. [ ] Freeze the engine/source revisions and inspect selected distribution licenses/notices.
2. [ ] Qualify lifecycle/isolation before execution, then compare evidence, uncertainty and incremental equivalence.
3. [ ] Measure each stage; compare native acceleration only on equivalent facts.
4. [ ] Test optional setup and missing-backend behavior through all three harnesses.
5. [ ] Record the selected engine, rejected alternatives and actual qualification results.

## T006 source screening (2026-10-06)

The [machine-readable decisions](../../evaluations/code-understanding/engine-decisions.json)
pin source references, constraints, proposed commands and missing component gates.
No engine, installer or candidate build was executed. Bounded private preparation
acquired the exact source and verified tool assets, then stopped at an unresolved
Go asset notice binding. JDK availability was checked; it is not engine
qualification. T004's independent AI-reviewed source key is the
comparison input, under its approved exception; human UX evidence remains separate.
This ADR remains Proposed and no structural engine is selected.

| Candidate | Disposition | Verified source constraint |
|---|---|---|
| codebase-memory-mcp `268a9d8886642eb7f9b2ce45f5ce27cdecf0f519` | Reject before execution as the complete structural evidence backend | MIT root license and vendored notice inventory exist. Internal calls carry exact spans, but both CALLS exporters omit them. Unresolved ordinary calls leave no site record. |
| Joern `b9e0ce2279b862615b07ed1afb0552502fe3ac05` | Evaluate; installation qualification blocked before build | Apache-2.0 root license; finite frontend, `--script` and export interfaces. Exact Go asset license/notice binding remains unresolved. No structural owner or incremental backend is qualified. |

Codebase-memory's [internal call representation](https://github.com/DeusData/codebase-memory-mcp/blob/268a9d8886642eb7f9b2ce45f5ce27cdecf0f519/internal/cbm/cbm.h#L274-L313)
has `site_start_byte` and `site_end_byte`. The
[sequential exporter](https://github.com/DeusData/codebase-memory-mcp/blob/268a9d8886642eb7f9b2ce45f5ce27cdecf0f519/src/pipeline/pass_calls.c#L510-L524)
and [parallel exporter](https://github.com/DeusData/codebase-memory-mcp/blob/268a9d8886642eb7f9b2ce45f5ce27cdecf0f519/src/pipeline/pass_parallel.c#L2157-L2175)
retain call lines and resolver metadata but omit those spans. The
[parallel unresolved branch](https://github.com/DeusData/codebase-memory-mcp/blob/268a9d8886642eb7f9b2ce45f5ce27cdecf0f519/src/pipeline/pass_parallel.c#L3214-L3243)
drops ordinary unresolved calls. A candidate count is not a target set, and a
missing graph edge cannot account for every frozen unknown site. Reconstructing
these facts with another parser does not qualify this engine as their owner.
Reconsideration requires a supported occurrence export that preserves the facts.

The source corrects the earlier lifecycle rejection: the documented
[runtime rendezvous setting](https://github.com/DeusData/codebase-memory-mcp/blob/268a9d8886642eb7f9b2ce45f5ce27cdecf0f519/docs/CONFIGURATION.md#L151-L194)
and [POSIX endpoint](https://github.com/DeusData/codebase-memory-mcp/blob/268a9d8886642eb7f9b2ce45f5ce27cdecf0f519/src/daemon/ipc.c#L965-L1007)
support a distinct coordination directory. Cohort and project locks use that
endpoint directory. These findings establish source support for isolation;
coexistence and cancellation remain unmeasured. They do not repair the evidence
contract failure or authorize account-wide activation.

Joern's supported source build is `sbt createDistribution` at the pinned commit.
The [build](https://github.com/joernio/joern/blob/b9e0ce2279b862615b07ed1afb0552502fe3ac05/build.sbt#L1-L82)
uses Scala 3.8.3 and CPG 1.7.78; `project/build.properties` pins sbt 2.0.10.
The [root requirements](https://github.com/joernio/joern/blob/b9e0ce2279b862615b07ed1afb0552502fe3ac05/README.md#L30-L34)
specify JDK 21. JDK 21.0.12.1 is available in the screening environment; sbt and
Joern tools were absent from PATH. The bounded recent-100-tag inspection found
no exact-pin tag. Reviewed releases v4.0.649 and v4.0.648 resolve to different
commits, so their ZIPs cannot substitute for this candidate. The private build
must retain binary/JAR/dependency identity and notices, including JS astgen
3.50.1 and goastgen 0.1.0. Distribution assembly stages additional frontends;
report and bound that cost. No installer, plugin updater or live configuration
change belongs to the experiment.

The coordinator approved bounded private preparation: two CPUs, Java
`ActiveProcessorCount=2`, at most 4 GiB heap, 20 minutes wall time and 4 GiB disk
growth, with private user/cache/boot/global/Ivy/Coursier/temp directories. These
are approved build limits, not measured engine results. Preparation verified a
clean checkout at the exact Joern pin and acquired the official sbt 2.0.10 TGZ,
JS astgen 3.50.1 Linux binary and Go astgen 0.1.0 Linux binary. Actual hashes and
retained notices are in the machine-readable decisions. No build started.

The [exact Go tag](https://github.com/joernio/astgen-monorepo/tree/db450070a4242b8196d0d97ecf4434d9b6910e35)
has no LICENSE/NOTICE in its bounded complete inventory; its release body is
empty and the archived original repository also exposes no license. The later
monorepo root license was introduced separately. The licensed JS-tag monorepo
changes a Go implementation file and its
[release instructions](https://github.com/joernio/astgen-monorepo/blob/a43e12b79d0be1b4fe47614ae4b1856aacbd6796/README.md)
permit rebuilding existing tags. That later root license and the version label
do not establish the reviewed binary's source/notice binding. Installation
qualification needs primary license/notice and provenance evidence for
`goastgen-linux` SHA-256
`cc2a1e1d5f613e8c6fea4c9ef2002b4e6f079164671c21e15b065f45bf6d506e`,
including its dependency notices. This is an unresolved qualification input,
not a legal conclusion. No alternate release or frontend subset was built.

Finite commands use explicit source/output paths and a trusted fixed
[`--script`](https://github.com/joernio/joern/blob/b9e0ce2279b862615b07ed1afb0552502fe3ac05/console/src/main/scala/io/joern/console/BridgeBase.scala#L65-L79)
or [`joern-export --out`](https://github.com/joernio/joern/blob/b9e0ce2279b862615b07ed1afb0552502fe3ac05/joern-cli/src/main/scala/io/joern/joerncli/JoernExport.scala#L74-L112).
The exact script parser accepts repeated `--param key=value`. Each invocation
must have an owned working directory because the default workspace is
`baseDir/workspace`. Pin `SHIFTLEFT_OCULAR_INSTALL_DIR` to the private
distribution and verify private user/config/cache/temp roots. The lifecycle
record keeps coexistence, cancellation and cleanup unexecuted; source close
hooks do not prove cleanup after forced termination.
Run Python, Go, JavaScript and TypeScript separately. JS/TS share `jssrc2cpg` but
keep separate scores and enable file content for offsets. Python stores call
line/column/offset pairs; JS/TS offsets are conditional on file-content setup.
Go stores call start line/column but its shared CALL builder does not emit end
positions or byte offsets. Verify each range against original UTF-8 bytes;
missing/unverifiable ranges remain unsupported. CALL nodes exist before linking,
so export unknown/unlinked calls, candidates and skipped files. Name/type recovery
or static dispatch does not prove an exact binding. All frozen constructs still
require actual per-case comparison.

Raw repository-directory execution is ineligible because
[`SourceFiles.determine`](https://github.com/joernio/joern/blob/b9e0ce2279b862615b07ed1afb0552502fe3ac05/joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/SourceFiles.scala#L243-L259)
defaults to following links. Use a confined immutable regular-file snapshot,
preserve the original path/hash inventory and stage JS/TS under neutral roots
to account for default test-path exclusions. Pin sidecar paths/hashes rather than
resolving ambient tools, disable dependency fetching, and make source inputs
read-only. Private output, workspace, caches and temp paths are required.

The [subprocess helper](https://github.com/joernio/joern/blob/b9e0ce2279b862615b07ed1afb0552502fe3ac05/semanticcpg/src/main/scala/io/shiftleft/semanticcpg/utils/ExternalCommand.scala#L18-L87)
defaults to infinite waits; its finite timeout does not prove descendant cleanup.
An isolated process group or cgroup must enforce cancellation, reaping and
resource/output limits without touching existing sessions. Fixed queries must
count examined work and enforce the ADR 0005 budgets inside traversal. A whole
GraphSON export is permitted only for the finite locked fixture with a file-size
cap; exporting everything then trimming is not a bounded query proof.

After the asset prerequisite and exact build are resolved, the artifact identity,
confinement and counted-query/cancellation plan require coordinator review before
frontend execution. Then compare the same 44 fixture cases,
16 AI-reviewed real-call sites and 16 update scenarios with the native baseline,
retaining every unsupported or unknown outcome. Rebuild mutated snapshots and
report the full cost: no supported incremental batch API was established.
Structural selection still needs every ADR 0006 component gate.

| Optional choice | Decision | Reopening condition |
|---|---|---|
| Existing SCIP enrichment | Evaluate | Source-bound index artifacts, callsite syntax and measured precision benefit |
| Joern depth comparison | Evaluate | The confined finite experiment above passes its setup/lifecycle gates |
| Additional graph database | Reject for first slice | Measured required workload exceeds current storage |
| General DSL/MCP | Defer | Concrete caller need and query/trust qualification |
| WebGL renderer | Defer | Existing viewer fails a measured scale/UX requirement |
| ANN index | Defer | Exact search misses measured required latency/RSS budgets |
| Embedding/reranker alternatives | Defer | Separately authorized pinned local retrieval comparison |
| Custom Rust/PyO3 | Defer; no prototype | Profiling identifies the limiting stage, reuse cannot solve it, and a prototype is separately authorized |

None of these options becomes automatic scope. Custom Rust retains the ADR 0006
equivalent-workload threshold: at least 20% end-to-end improvement without more
than 10% warm-path or peak-memory regression, preserving correctness. These are
targets, not measured results.
