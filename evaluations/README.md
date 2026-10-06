# Repository retrieval and UX evaluations

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
