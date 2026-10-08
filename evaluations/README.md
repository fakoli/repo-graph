# Repository retrieval and UX evaluations

## Code-understanding input preparation

The synthetic [fixture manifest](code-understanding/fixtures.json) records
separate Python, Go, JavaScript and TypeScript cases, source ranges, uncertainty,
questions and update scenarios. [Corpus pins](code-understanding/corpora.json)
and [real-call candidates](code-understanding/real-calls.json) retain all sixteen
selected sites. Their original proposed targets remain unreviewed metadata;
the separately locked source key records actual independent AI judgments.
Finite candidate incremental equivalence has been measured; product scale and
independent human evaluation remain unqualified.

Check the prepared inputs:

```bash
uv run python evaluations/acceptance.py --prepare
```

This checks full source hashes, UTF-8 ranges, target IDs, explicit uncertainty
and the committed input lock. A preparation success does not pass the freeze
gate. Run its regression checks with
`uv run python -m unittest discover -s evaluations/code-understanding -p test_acceptance.py -v`.

The user authorizes an independent Astra AI source reviewer for the sixteen
initial real-call sites. The locked [policy](code-understanding/source-review-policy.json)
and [source key](code-understanding/source-review.json) record that exception.
The validator accepts the specified independent AI provenance and rejects human
labels, missing/model-mismatched reviews, incomplete judgments or changed pins.
The coordinator audits the actual delegated review receipt. These comparisons
are against an AI-reviewed source key; they are not independent human results.

Lock reviewed inputs with `--prepare --seal-inputs`, review and commit them,
then run the required gate:

```bash
uv run python evaluations/acceptance.py --gate freeze
```

Every locked input must match one captured Git commit. Preparation never passes
T004. Changed judgments require a new reviewed lock. Missing independent review
leaves comparisons unrun. Reports retain actual per-check outcomes and source
receipt hashes. ADR 0006 numeric targets remain acceptance targets, not results.
T027 independent human UX and human agent-answer grading remain separate gates.

## Native baseline and reusable-engine screening

Install the optional comparison backend in an isolated checkout environment:

```bash
uv sync --python 3.12 --extra analysis
uv run python evaluations/analysis.py --engine tree-sitter --suite component
uv run python evaluations/analysis.py --screen-engines
```

The [native component report](results/code-understanding/native-component.json)
grades 75 selected definitions and 28 supported direct/reference bindings in
Python, Go, JavaScript and TypeScript. It retains twelve unknown sites and four
conservative receiver sites. All four receiver target-enumeration checks fail;
their eight missing alternatives are recorded individually. This is a syntax
and direct-binding baseline, not a qualified structural owner or a complete
definition census. The scanner reads source inventory and bytes without gold
targets; the separate grader matches exact UTF-8 ranges after extraction.

The [source-screen report](results/code-understanding/reusable-screen.json)
records pinned primary sources and evaluate/defer/reject decisions. At the
screened revision, codebase-memory's supported graph exports omit required
callsite spans and unresolved sites. Joern remains a comparison candidate,
with installation blocked before build by its exact Go asset notice/provenance
gap. No reusable engine was executed. Optional alternatives are recorded
decisions, not installed features.

The [task report](results/code-understanding/engine.json) preserves the frozen
T004 evidence and separate T005/T006 scopes. Passing either command does not
select an engine, prove incremental updates or satisfy agent/human gates.
Installed native wheels were executed on Linux x86_64 with Python 3.12; other
platforms remain unqualified. Core mapping and keyword search add no dependency.
Missing or different backend versions return an explicit blocked result and
never install packages automatically.

Focused checks:

```bash
uv run python -m unittest discover -s evaluations/code-understanding -p test_analysis.py -v
uv run python -m unittest discover -s evaluations/code-understanding -p test_performance.py -v
```

Structural profiling uses finite worker processes and actual resource counters.
Import maps and callable facts have different outputs; their timings cannot
establish an equivalent-workload speedup. A native unchanged repeat is currently
a full rescan. Custom Rust/PyO3 remains unbuilt and unadopted.

The persistent-index fixture pilot uses the same frozen source in serial and
queued modes, with body and export edits checked against clean rebuilds. Set
`REPO_GRAPH_EVAL_WORK_ROOT` to an existing private evidence directory outside
the checkout, then run:

```bash
uv run python evaluations/analysis.py --profile-pilot
```

This command requires committed evaluation code and the optional analysis
backend. It retains raw receipts privately and writes a separate bounded
`persistent-pilot.json` report. It never accepts T008 or freezes resource budgets.
Representative corpus measurements, complete source I/O accounting and
independent correctness/adversarial review remain required for qualification.

The experimental Django runner takes a separately reviewed private protocol
directory containing the pinned header, streamed content manifest, six query
selectors and two independent postimages. With the private source map and work
root configured below, its command is:

```bash
uv run python evaluations/analysis.py --profile-pilot --protocol path/to/reviewed-protocol
```

Only the preregistered first repetition is admitted. Correctness and adversarial
preflight approval must precede corpus execution. Every phase independently
captures SQLite and measures the five canonical fact streams, then writes
`persistent-Django.json` without replacing an existing report. Serial and queued
modes share the same copied source owner. The run stops for evidence review
before repetitions two and three; it does not qualify the remaining corpora,
adopt Rust, select an engine or freeze resource budgets. Storage ceilings are
cooperative measurement stops; sampled owned RSS is not an exact peak or a
kernel memory quota. The evaluator does not flush the OS cache.
The work root must be outside every mapped corpus. Each controller has a hard
job deadline within the pair deadline. Outer group cleanup covers only that
group; absent collector receipts leave descendant cleanup unknown and stop
further admission. Each captured snapshot must match all five identities in
its ready refresh receipt, including generation and source.
The revised private protocol preserves the failed first attempt and corrects
only independently reviewed source selector bounds. After a phase's full
measurement, equal identities, counts and canonical digest may share a verified
earlier sealed snapshot. Its own snapshot timings, size and digest remain in
the phase receipt. Obsolete evaluator-owned live indexes are removed only after
their retained evidence is validated. The 4 GiB job and 8 GiB pair caps remain;
this storage policy is a measured-size feasibility correction, not qualified
capacity. Failed controller causes and actual source cleanup remain observable
when execution stops. Live storage scans retain gaps when a worker removes a
listed request or publishes a temporary file before its metadata is read.
These scans observe allocated lengths without claiming an atomic total; other
metadata and ownership errors still stop the run. Use a distinct `--output`
path for the revised attempt; previous reports are never overwritten.

For the pinned local corpus checkouts, supply a private JSON source map with
`schema_version: 1` and a `corpora` array of `{id, source, revision}` entries.
`source` is an absolute checkout directory; `revision` is its full Git commit.
Use the committed corpus IDs/revisions and any independently reviewed dependency
source. Keep this local map outside the product and source checkouts. Set
`REPO_GRAPH_EVAL_SOURCE_MAP` to its filename, then run:

```bash
uv run python evaluations/analysis.py --compare --suite component
uv run python evaluations/acceptance.py --gate engine
uv run python evaluations/analysis.py --profile --freeze-budgets
uv run python evaluations/acceptance.py --gate acceleration
```

These commands currently return a blocked result. The comparison retains each
real-call judgment and finite worker check. Its serial and queued candidates
share the collector, resolver, source identity, incremental publication and
bounded query implementation. Serial uses one worker; queued accepts one to
four workers. Failed, cancelled or exhausted updates preserve the prior ready
generation. Both modes are required, regardless of relative speed.

Optional collector telemetry records backend setup, parsing, traversal/lowering,
handoff and controller timings outside the structural facts and cache payloads.
Source-free readiness events register verified owned processes before parsing;
observer refusal, overflow or failure stops the measurement while retaining
owned cleanup. These observations do not establish resource defaults. A blocking
callback or filesystem operation still requires a finite outer supervisor.

The retained component run includes 36 updates matching clean rebuilds in both
modes, 20 physical query checks and nine actual missing-backend checks. Acceptance
recomputes these observations from hashed private source/fact/worker archives;
a passing report label cannot qualify them. These are finite component results,
not large-repository, installed-harness or human qualification. Public scan/index
commands will expose mode selection after structural-owner qualification.

The four-corpus profiler
runs three independent workers per workload, each capped at 4 GiB address space
and 660 seconds, and retains failed/partial trials. Capacity reports have no
equivalent-fact reference, update/query measurements or accepted budget lock.
Neither relabeling a report nor a requested budget freeze can pass those gates.
Current implementation bytes and committed frozen inputs must match the reports.

Workers use private home/config/cache/temp directories and isolated Python with
bytecode writes disabled. Full profiling logs and inventory receipts remain
beside the private source map; portable reports retain their digests, individual
resource measurements and coverage failures. Worker directory creation and
writes use held directory descriptors on Linux. Other kernels remain unqualified.

Re-export a complete private profile without rerunning its workers:

```bash
uv run python evaluations/analysis.py --profile --profile-report PRIVATE_REPORT.json
```

The exporter verifies the recorded Git implementation and corpus/map identities,
retains all twenty-four trials and distinguishes its own reporting code from the
measured implementation. Full file receipts remain in the hashed private archive;
the bounded portable report lists each failed file's path and status. Missing
resource counters after native crashes remain unavailable. Archived measurements
cannot establish qualification of a changed analyzer.

The [three-trial capacity report](results/code-understanding/capacity-profile.json)
retains twenty-four workers measured at `d6413d5`. Fifteen exited successfully
with partial coverage; nine native workers failed. The compact archive export
uses separately identified reporting code and does not rerun those workers.

| Corpus | Workload | Fresh median seconds | Largest reported fresh process RSS, bytes | Worker outcomes |
| --- | --- | ---: | ---: | --- |
| Django | Import map | 1.548 | 67,723,264 | 3 partial |
| Django | Native callable facts | 16.570 | 1,675,739,136 | 3 partial |
| Odoo | Import map | 6.861 | 300,933,120 | 3 partial |
| Odoo | Native callable facts | Unavailable | Unavailable | 3 SIGSEGV |
| AWS provider | Import map | 6.544 | 458,387,456 | 3 partial |
| AWS provider | Native callable facts | Unavailable | Unavailable | 2 exit 1, 1 SIGSEGV |
| Kubernetes | Import map | 6.446 | 608,202,752 | 3 partial |
| Kubernetes | Native callable facts | Unavailable | Unavailable | 3 exit 1 |

These workloads produce different facts, so their times cannot establish a
speedup. Missing counters after crashes remain unavailable. Equivalent facts,
incremental updates, bounded queries and both required serial/queued modes
still need qualification; resource budgets and Rust adoption remain blocked.

The [single-worker scale pilots](results/code-understanding/structural-pilots.json)
retain the initial native crash and fixed-run results. Django finished a partial
scan in 15.4 seconds at 1,981,386,752 bytes peak RSS, with three partial parses.
Odoo failed with `MemoryError` under the 4 GiB address-space cap. These pilots
do not pass scale, incremental or acceleration qualification; no final resource
budgets have been frozen.

## Unified harness packaging (0.6.0)

`repo-graph init` delegates installation to native Pi, Codex and Claude managers.
The default pin follows the product version. Focused unit checks cover missing
CLIs, invalid inputs, scope rejection before mutation and partial failures.
`tests/pi_smoke.py` and `tests/harness_smoke.py` exercise actual native discovery
and packaged script execution in temporary homes without provider requests.
The Codex/Claude checks also repeat initialization, verify version-changing
upgrade/rollback and check the shared skill.
A built wheel is installed in a temporary environment and its CLI/asset presence
checked before release. These are packaging checks, not model task or human UX
evaluations. Existing Node/browser checks continue to cover the viewer.

Polyglot function analysis remains proposed. Its corpus, uncertainty, incremental
correctness, agent task and human navigation gates are specified in
[the feasibility report](../docs/polyglot-feasibility.md) and
[ADR 0003](../docs/adr/0003-polyglot-fact-index.md).

## Jev and local reranker comparison (0.5.0)

[Research, decisions and limitations](../docs/jev-research.md),
[AWS results](results/jev-aws.json), [Kubernetes results](results/jev-kubernetes.json).
The original development queries and [fresh frozen queries](jev-queries.json)
share the same 32-candidate pool across methods. Eight cases per corpus/set is
a small developer-authored evaluation, not a blinded independent holdout. Reranker
judgments are not the grades. Hit@5, MRR, candidate coverage, latency, cache repeats
and actual API token usage are retained, including misses and regressions.

```bash
uv run --extra semantic python evaluations/rerank.py terraform-provider-aws OUTPUT --source SOURCE --report REPORT.json --jev
uv run --extra semantic python evaluations/rerank.py kubernetes OUTPUT --source SOURCE --report REPORT.json --jev
uv run python evaluations/jev_probes.py --report PROBES.json
```

The first two commands require an existing complete semantic index and cached
local reranker (`repo-graph index OUTPUT --reranker`). `--jev` explicitly exports
bounded excerpts from the public corpus and incurs cost; omit it for local-only
comparison. The probe command sends synthetic examples to Jev. Existing request
caches make reruns cheaper; a report from a cached run is not a fresh API latency
measurement. Successful calls, attempts, failures and unknown token usage must
be distinguished. Pricing estimates exclude developer time and model setup.

Across 32 queries, hybrid hit@5 was 18/32, MiniLM 20/32 and Jev 23/32. On fresh
queries Jev was 14/16 versus hybrid 10/16; the old Kubernetes hit@5 was unchanged
and AWS fresh MRR worsened. The 32 live requests used 350,445 input tokens,
estimated $0.01472 at the 2026-10-06 published rate, with no fallback. Every
repeat used zero API calls. Small synthetic order/batching/injection probes are
in [jev-probes.json](results/jev-probes.json); they do not establish security.

The browser harness supports `REPO_GRAPH_UX_RERANK=local|jev|none`. Jev mode
requires an approved public corpus for live runs. [AWS Jev](jev-aws-ux.json) and
[Kubernetes local](jev-kubernetes-ux.json) passed all views, result navigation,
query/method restoration and a narrow viewport, with zero browser errors.
Default synthetic CI uses no Jev calls or credentials.

## Generation and search speed comparison

[Experience/performance research](../docs/graph-experience.md) links four
before/after reports. Run against a pinned public source and an existing complete
semantic output, with optional `--compare BASELINE.json` to require identical
structure/results:

```bash
uv run --extra semantic python evaluations/performance.py kubernetes OUTPUT --source SOURCE --report REPORT.json --runs 3
```

The harness creates temporary fresh mapping outputs, checks unchanged repeat-map
reuse, times 48 repeats per mode across the 16 frozen queries, and records exact
implementation/model hashes. Fresh output means empty scanner/search caches;
OS source caches may be warm. Existing vectors are reused, not re-embedded.

## Original retrieval benchmark

Judgments: [queries.json](queries.json). Method and gates:
[research.md](../docs/research.md#evaluations). Reports are committed only after
all benchmark stages complete. Keep misses and mode comparisons; do not silently
change judgments to accommodate retrieval failures.

Map each pinned public source checkout into a separate output directory, then
embed it with `repo-graph index OUTPUT --semantic`. Run:

```bash
uv run python evaluations/run.py terraform-provider-aws OUTPUT --source SOURCE --report evaluations/results/aws.json
uv run python evaluations/run.py kubernetes OUTPUT --source SOURCE --report evaluations/results/kubernetes.json
```

Re-map and re-index to measure reuse. The benchmark freezes source commits,
query judgments and the embedding model. Warm p50/p95 excludes initial model
load; record CLI cold time and indexing separately. Corpus output is local and
not included in published reports. Returned public paths and metrics are safe
to retain, with attribution to the source repository.

Run browser checks against either generated large map:

```bash
REPO_GRAPH_PYTHON=.venv/bin/python REPO_GRAPH_UX_OUTPUT=OUTPUT REPO_GRAPH_UX_QUERY='access permissions' REPO_GRAPH_UX_REPORT=REPORT npm run test:ux
```

The fixture test covers all diagram views, bounded node counts, search-to-file navigation,
query restoration, a narrow viewport and browser errors. It records a screenshot
and initial load time. It is an automated workflow evaluation, not a human study.
Semantic relevance is evaluated separately with explicit expected source areas.

A separate synthetic scan benchmark is reproducible with
`uv run python evaluations/scale.py --documents 100000 --report REPORT.json`.
It isolates SQLite/vector scan cost with repeated normalized vectors; it does
not establish retrieval quality, embedding throughput or million-file support.

## Measured 0.4.0 baseline

These are development benchmarks measured on Linux x86_64 with four ONNX CPU
threads and BGE-small-en-v1.5, not an independent holdout or human UX study.
All source revisions, model hashes, per-query returned paths and missed judgments
are retained in the linked reports. Keyword results informed extraction fixes;
judgments were not changed to make retrieval pass.

| Public corpus | Files / vectors | Keyword hit@5 | Semantic hit@5 | Hybrid hit@5 | Hybrid warm p95 | Cold CLI |
|---|---:|---:|---:|---:|---:|---:|
| [Terraform AWS](results/aws.json) | 20,370 | 50.0% | 37.5% | 62.5% | 81.0 ms | 0.804 s |
| [Kubernetes](results/kubernetes.json) | 25,788 | 25.0% | 25.0% | 37.5% | 81.6 ms | 0.766 s |

**The 75% hybrid relevance target failed on both corpora.** Semantic search is
experimental. Hybrid improved over keyword on these queries, but metadata-only
paths, duplicate fixtures and early-file summaries crowded out implementation
areas. Expected areas are not exhaustive: some returned tests and adjacent
components can be useful despite counting as misses. The next experiment is
richer code chunks and result diversity, with a new independent query set.
The measured latency does not justify adding an approximate vector index yet.

[Reuse measurements](reuse.json): unchanged maps took 1.960 s / 2.326 s and reused
all 20,370 / 25,788 evidence summaries and vectors. Re-indexing with the model
already loaded took 0.063 s / 0.054 s and embedded zero new files. Observed initial
embedding runs took 1,265 s / 1,149 s; they ran concurrently and the corpus was
expanded during the run, so these are operational observations, not controlled
cold-index throughput benchmarks. Model download/load is excluded from those
index timings. CLI measurements include process startup and cached model load.

[Browser AWS report](aws-ux.json) and [Kubernetes report](kubernetes-ux.json)
passed all seven views, node bounds, downloads, hybrid search-to-file navigation,
query restoration and an 800 px viewport with zero browser errors. Initial
loads were 176 ms / 185 ms. The checks use one explicit query per corpus and do
not judge relevance. [AWS screenshot](aws-ux.json.png),
[Kubernetes screenshot](kubernetes-ux.json.png).

The [100,000-document synthetic vector scan](synthetic-scale.json) took a median
167 ms with a 211 MB SQLite file. Repeated vectors isolate exact scan cost;
this is not a real 100k-file monorepo relevance test. Million-file support remains
unqualified. Native Pi discovery and bounded keyword retrieval passed with no
provider calls; agent instruction-following was not graded.

## Supplemental real-call target locations

The original sixteen AI-reviewed source judgments remain unchanged. The
[supplemental location key](code-understanding/source-target-locations.json)
records exact declaration boundaries, actual identifiers and explicit anonymous
callable alternatives. A separate
[supplemental lock](code-understanding/source-target-lock.json) binds the original
input hashes and independent input audit. Its committed lock supersedes the
supplement artifact's historical `freeze_review_pending` preparation label.
The original ungraded comparison remains evidence of the earlier key limitation.

These positions are consulted only by the grader after source-only extraction.
They do not create target facts, change the supported denominator, select an
engine, or satisfy the human evaluation gate.
