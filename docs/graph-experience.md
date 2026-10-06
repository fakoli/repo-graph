# Graph experience and performance research

2026-10-06. The work preserves the local scanner, bounded SVG views and SQLite
index. Research used Exa to find primary implementations and product docs;
design critique and accessibility review guided the interface changes.

## Ideas worth adapting

| Primary source | Useful idea | Decision here |
|---|---|---|
| [GitDiagram](https://github.com/ahmedkhaleel2004/gitdiagram) | Start with system areas and link components back to real source paths | Preserve the System overview and strengthen navigation/selection; no generative graph replacement |
| [Graphify](https://github.com/Graphify-Labs/graphify) | Distinguish extracted/inferred edges, query scoped subgraphs and reuse changed-file extraction | Preserve the observed-import/source-area boundary and bounded scopes; no whole-graph model context |
| [GitNexus](https://github.com/abhigyanpatwari/GitNexus) | Precompute structure and retrieve bounded, grouped context; incrementally update an index | Preserve cached extraction and hybrid retrieval; avoid adding another database/parser stack for current needs |
| [Sourcegraph](https://sourcegraph.com/docs/code_search/reference/queries) | Make search scope distinct from the query pattern | Expose the existing source-prefix filter in the viewer |
| [Sigma.js](https://www.sigmajs.org/) | Emphasize selected neighborhoods; WebGL is useful for genuinely large visible graphs | Keep SVG for our 24-node scopes; prioritize legible selection and links |
| [Cytoscape.js](https://github.com/cytoscape/cytoscape.js/blob/master/documentation/md/performance.md) | Direct ID lookups, batched modifications and careful label/edge cost | Preserve ID maps and bounded scenes; avoid unreadable labels and unnecessary full-scene work |

These are design/implementation precedents, not comparative product benchmarks.
No source code from these projects was copied. They have different evidence
models, deployment requirements and licenses; their feature claims do not prove
runtime call correctness in this scanner.

## Interface findings and priorities

The baseline large-map screenshots showed fitted System cards at 53% zoom,
leaving roughly 9 px title text, truncated source names and dense crossing links.
An empty desktop inspector reserved 298 px even before selection. This made
orientation harder despite a fast page load.

| Finding | Impact | Change |
|---|---|---|
| Small fitted cards and large vertical gaps | Components are difficult to read at first glance | Compact System spacing and reclaim the empty inspector area |
| Location text is truncated and not actionable | Users lose their place after drilling down | Semantic home/breadcrumb navigation with complete path labels |
| Search result opens a file without moving focus | Keyboard users lose orientation | Focus and center the selected file; preserve search state |
| Focus is unclear on SVG cards and tabs | Exploration is difficult without a mouse | Visible focus, keyboard controls and tab semantics |
| Narrow controls compete with component list | Important navigation is clipped | Reflow the layout and controls at narrow widths |
| Source filtering exists only in the CLI | Broad queries return avoidable unrelated candidates | Visible source-prefix search scope |

The automated review covers practical keyboard/focus, labels, layout and rendered
geometry. It is not a complete WCAG certification or manual screen-reader study.

At 1440×1000, minimum rendered System titles increased from 9.05 to 14.95 px
for AWS and 8.70 to 15.83 px for Kubernetes. Reclaiming the empty inspector
increased the canvas from 874 to 1172 px. Crowded System layers wrap into two
columns; all 12 cards remain inside the usable viewport. Selected connection
labels are shown with the selected neighborhood rather than crowding the overview.
Measured text contrast in the checked labels is at least 6.02:1.

[AWS baseline](../evaluations/ux-0.5-aws-baseline.json),
[AWS after](../evaluations/ux-0.5-aws.json),
[Kubernetes baseline](../evaluations/ux-0.5-kubernetes-baseline.json),
[Kubernetes after](../evaluations/ux-0.5-kubernetes.json) retain the measurements.
Each final large-map run passes 21 checks, including all views, readable initial
geometry, keyboard tab/pan/zoom/fit, scoped search, breadcrumbs, focused source
selection and a 360 px search/details workflow. Zero browser errors were observed.
Page loads were 135/147 ms and source navigation 58/57 ms in the final comparable
keyword runs; these single workflow observations are not a latency distribution.
[AWS screenshot](../evaluations/ux-0.5-aws.json.png),
[Kubernetes screenshot](../evaluations/ux-0.5-kubernetes.json.png).

## Performance hypotheses and results

Profiling found repeated path construction in the synopsis line loop, repeated
length calculation, duplicate file-stat work, and a second full pass over vector
readiness before the actual scan. The changes hoist invariant work, reuse one
stat result, use indexed prefix bounds, detect missing vectors in the actual scan
and skip scores that cannot enter the current heap. Tied scores retain the
original ordering. The 512-vector memory bound remains; no approximate index,
corpus-wide vector cache, new dependency or serving change was needed.

| Public corpus | First map before → after | Repeat map before → after | Hybrid median before → after | Hybrid p95 before → after |
|---|---:|---:|---:|---:|
| AWS, 20,370 files | 12.964 → 6.543 s | 1.769 → 1.512 s | 58.42 → 41.49 ms | 71.48 → 53.88 ms |
| Kubernetes, 25,788 files | 10.792 → 6.272 s | 2.153 → 1.883 s | 65.21 → 46.32 ms | 71.92 → 52.95 ms |

First-map medians improved by 49.5% and 41.9%; repeat maps by 14.5% and 12.5%.
Hybrid medians improved about 29%. Each mapping stage has three samples;
each search mode has 48 timed repeats across 16 frozen queries. First map means
an empty output/index/scan cache, not an uncached operating-system disk read.
Source page caches may be warm. Before/after runs are sequential observations
on the same Linux CPU host, not a throughput or hardware comparison.

Both source commits, model hash, runtime, raw samples and results are retained:
[AWS before](../evaluations/results/performance-aws-before.json),
[AWS after](../evaluations/results/performance-aws-after.json),
[Kubernetes before](../evaluations/results/performance-kubernetes-before.json),
[Kubernetes after](../evaluations/results/performance-kubernetes-after.json).
Structure fingerprints and all returned search results across keyword, semantic
and hybrid modes remained identical. The complete existing embeddings were
reused; this experiment does not measure embedding generation throughput.

The loopback server now keeps status, artifacts and plain keyword requests
responsive during a slow model judgment. A single nonblocking slot bounds
expensive searches; another model search receives an explicit busy response.
A synthetic blocked-judgment check exercises this behavior without network calls.

## Jev decision and remaining ceilings

[Jev research and evaluations](jev-research.md) support optional shortlist
reranking, not a new default. Fresh hit@5 rose from 10/16 to 14/16, but older
Kubernetes hit@5 was unchanged and some ranks worsened. API export remains
explicit, bounded, cached and validated. Astra's review found malformed HTTP
fallback and unvalidated response-cache fields; both were corrected and checked
with synthetic failures before release.

Keep richer evidence and candidate diversity as the next recall experiment.
Million-file repositories, runtime call flow, human task-completion savings and
assistive-technology coverage remain unqualified. Add a larger model, parser or
ANN index only when a frozen real workload demonstrates the missing benefit.
