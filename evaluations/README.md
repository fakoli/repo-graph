# Repository retrieval and UX evaluations

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
