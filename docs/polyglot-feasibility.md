# Polyglot code understanding: feasibility and evaluation

2026-10-06. **Status: proposed static-analysis design, not an implemented call
graph.** Repo Graph currently inventories files, extracts heuristic local imports
for Go, Python, JavaScript and TypeScript, and aggregates them into source-area
diagrams. Other languages remain visible as paths. Its search index contains
bounded file evidence; it does not resolve function calls or prove runtime flows.

## Recommendation

Use one persistent fact index for diagrams, structural queries and semantic
retrieval. Separate file extraction from relationship resolution: parse each
changed file once, then perform the necessary resolution passes over stored
facts. Language-specific grammar and binding rules belong at this boundary;
the storage, query interface and views remain shared. One product does not
require one identical semantic rule for every language.

Evaluate an existing engine before implementing a new resolver. The first
candidate is [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp),
with a pinned version and independent accuracy checks. Preserve Repo Graph's
existing lightweight map/search path during that experiment. A language rewrite
is justified only if the measured indexing cost or engine integration requires
it; Python orchestration can already use native parsers and SQLite.

### What “supports a language” should mean

| Coverage level | Useful output | Evidence required |
|---|---|---|
| Inventory | Paths, source areas, configuration | File accounting, exclusions and unreadable-file reporting |
| Syntax | Definitions, imports, references, call sites | Grammar version, source ranges, parse errors and tested extraction rules |
| Binding | Targets for imports and names | Scope, alias, package and inheritance rules; ambiguity retained |
| Calls | Candidate callable targets | Receiver/type/assignment reasoning and explicit assumptions; unresolved sites retained |
| Framework/contracts | Routes, jobs, events, ORM models, service links | Tested framework patterns or explicit contracts, with evidence on both ends |

These levels need a published matrix per language and construct. A grammar count
establishes syntax availability, not resolved-call accuracy. A source-area map
can remain useful when a language has inventory coverage only.

Static analysis can conservatively approximate possible targets within stated
language and environment assumptions. A useful fast analysis may also miss
targets. Neither output means that every path executes. Reflection, monkey
patching, generated code, dependency injection, native extensions and unknown
runtime configuration prevent an unconditional “all business logic” promise.
Observed runtime calls, if imported later, are separate evidence and cover only
the exercised executions.

## Research that informs the design

| Primary source | Technique to reuse | Limit of the evidence |
|---|---|---|
| [Stack Graphs: Name Resolution at Scale, 2022](https://arxiv.org/abs/2211.01224) | Cache independently constructed file fragments and compose name-binding paths during queries | Name resolution is an ingredient of a call graph. Language binding rules still need implementation. The [official Rust implementation](https://github.com/github/stack-graphs) is dual MIT/Apache, but its README now says GitHub no longer supports or updates it. |
| [PyCG, ICSE 2021](https://arxiv.org/abs/2103.00587) | Track assignments between functions, variables, classes and modules to resolve Python call targets | Reported precision and recall are benchmark-specific; dynamic Python is not made complete by an assignment graph. Useful baseline for alias, closure and inheritance fixtures. |
| [Jarvis, revised 2024 paper](https://arxiv.org/html/2305.05949v5) | Demand-driven analysis from an entry function; per-function type information and flow-sensitive updates | Evaluated on 135 small Python programs and six applications. Entry-focused savings do not establish the cost of an entire monorepo or support for other languages. |
| [ACER, SCAM 2023](https://arxiv.org/abs/2308.15669) | A common Tree-sitter AST framework with language-specific call-graph rules | Its reported evaluation uses two context-insensitive Java generators. The framework's language interface is not an evaluation of arbitrary-language call precision. |
| [Soufflé, CAV 2016](https://souffle-lang.github.io/cav-paper) | Separate extracted facts from declarative analysis rules; compile expensive relational analyses | Large OpenJDK analysis demonstrates a scalable implementation technique, not a universal extractor. Adding a Datalog runtime is unnecessary until existing SQLite queries or a reused engine fall short. |
| [Scalable Call Graph Constructor for Maven, 2021](https://arxiv.org/abs/2103.15162) | Reuse partial library graphs and stitch them for the selected application | The proposal is an incremental Java CHA analysis with preliminary evaluation. Transfer the caching idea; validate language semantics and dependency-version identity separately. |
| [SCIP protocol and schema](https://github.com/scip-code/scip) | Import compiler/indexer-produced definitions, references and implementation relationships into a common model | SCIP occurrence roles do not by themselves distinguish every invocation from a callable value reference. Preserve call-site syntax and indexer provenance when deriving calls. |

[Tree-sitter](https://tree-sitter.github.io/tree-sitter/) supplies incremental,
error-tolerant syntax trees. It does not supply a universal name/type resolver.
Compiler-backed or SCIP indexes can improve precision where available, while a
syntax path allows discovery without running an unfamiliar repository's build.
Repository scanning should not automatically execute builds or install project
dependencies.

### Existing engine candidate

The [Codebase-Memory preprint](https://arxiv.org/html/2603.27277v1) describes a
Tree-sitter/SQLite pipeline, content-hash updates and structural MCP queries.
Its evaluated v0.5.5 system parses 66 languages. The authors report roughly ten
times fewer tokens, with answer quality 0.83 versus 0.92 for file exploration,
across 31 repositories. Grading included the first author; these are promising
author-reported results, not independent proof of call completeness or human UX.
The paper explicitly excludes reflection and dynamic dispatch from its static
structure and measures performance on one hardware configuration.

The [current implementation](https://github.com/DeusData/codebase-memory-mcp)
advertises a larger grammar set and more language-specific resolution than that
paper. Evaluate those capabilities at an exact commit; do not mix current
feature counts with older measurements. Its
[MIT license](https://github.com/DeusData/codebase-memory-mcp/blob/main/LICENSE)
makes it a reuse candidate. Review its
[third-party inventory](https://github.com/DeusData/codebase-memory-mcp/blob/main/THIRD_PARTY.md)
and the selected release's export/storage interfaces before bundling or
depending on it. Prefer a bounded fact import or supported engine invocation
over copying its daemon, UI, installer and harness configuration machinery.
The evaluated engine's own database can be the structural index if its interface
is sufficient; avoid a second persistent copy of every fact.

[Graphify](https://github.com/Graphify-Labs/graphify) remains a design and
comparison reference. Its current
[package metadata](https://github.com/Graphify-Labs/graphify/blob/v8/pyproject.toml)
identifies Apache-2.0; its
[NOTICE](https://github.com/Graphify-Labs/graphify/blob/v8/NOTICE) says only portions
from before relicensing remain MIT. Do not treat its retained MIT license file
as applying to all current code. This research imports no third-party code.

## Proposed common facts and resolution

Keep the first schema small; later framework facts must satisfy the same
evidence contract.

| Fact | Minimum recorded information |
|---|---|
| File | Repository revision, relative path, content hash, language, extractor version, extraction completeness/errors |
| Symbol | File identity, source range, kind, qualified name, containing scope and signature when available |
| Reference/call site | Source range, caller scope, expression/name, syntactic role and available receiver/type facts |
| Relationship | Relation kind, origin site, zero/one/multiple targets, resolution method, assumptions and supporting source ranges |
| Contract | Kind and scoped identifier, producer/consumer evidence, deployment/configuration uncertainty |

Expose `resolved`, `candidate`, `unresolved` and `observed` as distinct evidence
states. A heuristic score is not a probability. A uniquely chosen target is
still subject to the resolver's documented assumptions. Persist unresolved call
sites with reasons such as unknown receiver or excluded dependency, so absence
of an edge cannot silently look like absence of behavior.

Cross-language edges commonly join contracts rather than ordinary lexical
names: a TypeScript HTTP request to a Python handler, a protobuf RPC, a queue
producer to a consumer, or an FFI export to its binding. Matching a route string
alone can join unrelated services. Require contract/service scope and evidence
on both sides; retain multiple candidates when deployment routing is unknown.
Framework rules add facts to this shared index, rather than making a separate
scan and database for each view.

Jev or another model can summarize or rank already retrieved evidence. It must
not silently promote a guessed call target into an extracted fact. The current
[Jev boundary](jev-research.md) remains applicable.

### Scale and readability

1. Key reusable extraction by file content **and extractor/grammar version**.
   Changes to imports, exports, inheritance or package configuration invalidate
   affected derived bindings even when a caller's file is unchanged. A clean
   rebuild and an incremental update must yield equivalent facts.
2. Bound extraction workers, queue sizes and transaction batches. Reuse SQLite;
   keep writes controlled instead of accumulating the complete graph in worker
   memory. Record failed, skipped and oversized files. The current bounded
   synopsis must not be mistaken for complete symbol extraction from a large
   file.
3. Store a cheap broad structure first; refine a selected entrypoint or
   neighborhood when higher precision is needed. Report what remains unresolved
   when a time, node or dependency budget stops analysis.
4. Serve paginated scoped subgraphs and source excerpts. Aggregate into source
   areas for the overview, expand symbols on demand, and show hidden-neighbor
   counts. The existing 12-area System and 24-component scope limits are useful
   presentation bounds; they must not discard facts from the index.
5. Retrieve symbols with keyword/semantic search, then expand a bounded graph
   neighborhood. Measure relevant evidence per token and task success; sending
   the whole graph to a model defeats the intended efficiency.

This design needs multiple resolution passes; “one scan” means reusable source
extraction and one authoritative fact store, not an impossible single pass that
knows every cross-file target immediately.

## Evaluation before choosing an engine

The corpus choices below are frozen inventory snapshots, **not completed
static-analysis or performance benchmarks**. Counts were derived from complete
GitHub tree responses and exclude directories.

| Corpus | Pinned revision | Tracked files / Python files | Why it matters |
|---|---|---:|---|
| [Odoo 19.0](https://github.com/odoo/odoo/tree/2d9fd5562a0ef1f7f587eb393cc3bd293b047cca) | `2d9fd5562a0ef1f7f587eb393cc3bd293b047cca` | 48,249 / 8,675 | Business workflows across sales, accounting, inventory and manufacturing; ORM and framework behavior challenge ordinary name resolution |
| [Django](https://github.com/django/django/tree/3b7ae042cef02a09caab70ba54077a6f4cffac80) | `3b7ae042cef02a09caab70ba54077a6f4cffac80` | 7,085 / 2,933 | Framework dispatch, inheritance and a large test tree; test edges must remain distinguishable from production edges |

Start with a small synthetic polyglot fixture whose expected facts are written
before scoring: Python service, TypeScript caller, Go worker, protobuf/OpenAPI
contracts and a queue. Include same-name functions, import aliases, inheritance,
callbacks, multiple possible receivers, a missing dependency, a computed route,
a duplicate route in another service and an intentionally unresolved dynamic
call. Compare the reused engine, a language-specific Python baseline and the
current import map; these have different output capabilities.

| Question | Measurement / initial gate |
|---|---|
| Are the facts correct? | Definition, binding and candidate-target precision/recall by construct and language; require exact direct-call fixture targets and no falsely exact target for its ambiguous/dynamic cases |
| Does uncertainty survive? | Every fixture unresolved site and truncated/excluded file is reported; call references and runtime observations cannot be mislabeled as static invocations |
| Are updates correct? | Edit an exported name/type, delete/rename files and change a contract; incremental result equals a fresh rebuild, including dependent callers |
| Does it scale? | Cold index time, peak RSS, disk/fact counts, unchanged repeat and one-file/dependent updates; scoped query p50/p95, with budgets/exclusions recorded |
| Does structure help agents? | Frozen held-out tasks, identical harness/model settings, success, evidence correctness, actual tokens/tool calls and latency versus keyword/hybrid retrieval; retain each failure |
| Can humans use it? | Independent reviewers locate an entrypoint, follow a business path, explain an ambiguous edge and find a cross-language contract; record task completion/time and wrong conclusions, alongside existing keyboard/narrow-viewport regressions |

For Odoo, freeze reviewed questions such as confirming a sales order and
locating the resulting inventory/accounting hooks. Include framework-generated
targets and unresolved boundaries in the answer key. Runtime traces, if used as
a cross-check, only establish calls observed in those scenarios and are not an
exhaustive static ground truth. Keep corpora local and out of release archives.

Promote an engine only after these correctness and task checks pass and measured
cost fits an agreed machine budget. Establish large-corpus resource budgets from
the baseline before claiming a scale limit. Publish coverage and failures by
language/construct rather than one aggregate accuracy percentage. Until then,
polyglot call analysis remains proposed; existing diagram/search evaluations
continue to apply to the implemented product.
