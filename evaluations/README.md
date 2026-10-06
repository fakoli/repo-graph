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

The fixture test covers all modes, bounded node counts, search-to-file navigation,
query restoration, a narrow viewport and browser errors. It records a screenshot
and initial load time. It is an automated workflow evaluation, not a human study.
Semantic relevance is evaluated separately with explicit expected source areas.
