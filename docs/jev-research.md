# Jev and focused decision models for Repo Graph

Research and measurements: 2026-10-06. Implementation is optional; ordinary
mapping, keyword, semantic and hybrid retrieval still make no inference API calls.

## What Jev can contribute

Jev produces bounded decisions: Choice over named options, Score over an ordered
rubric, and Noul for yes/no probability. Questions in a request are independent;
their IDs are bookkeeping, so instructions must identify their evidence directly.
These properties fit shortlist judgments and finite labels. They cannot supply
free-form diagram summaries or new search phrases. [API](https://docs.typesafe.ai/api),
[generation limit](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

The current version is `jev-1.13.0`. Published input pricing is $0.042 per million
tokens, with output free. The context budget is 64k total and 32k for state plus
the longest question. State is processed once and questions fan out in parallel.
We pin the revision instead of following the moving alias. The provider states
that customer requests are not training data; this does not establish zero
retention for ordinary accounts. [Model reference](https://docs.typesafe.ai/models).

| Repository task | Fit | Decision |
|---|---|---|
| Reorder retrieved files by how directly their evidence answers a query | Strong candidate; measurable ranking task | Implemented and compared with a local reranker |
| Classify top-level source areas for the System diagram | Small advisory task | Existing labels now use the shared pinned, bounded client |
| Route a query through package boundaries | Plausible way to improve recall | Research candidate; requires a separate routing evaluation |
| Extract services, storage and protocols from documentation | Plausible finite schema | Compare a span extractor before adding a model dependency |
| Invent architecture descriptions or infer runtime calls | Unsupported by the current evidence/model | Keep source links and observed imports authoritative |
| Count files, compute edges, lay out diagrams or grade correctness | Deterministic code or independent evaluation | No model decision needed |

TypeSafe's reranking cookbook reports improvements on legal passages retrieved
with BM25, using an older Jev revision and per-passage judgments. This motivates
an experiment, not a code-search quality claim. Its hierarchical classifier keeps
multiple candidate branches rather than trusting one early decision. The example
does not establish large-monorepo recall. [Reranking example](https://docs.typesafe.ai/cookbooks/rerank_typesafe),
[hierarchical example](https://docs.typesafe.ai/cookbooks/hierarchical_classification).

An independent September 2026 preprint evaluates Jev on 37 datasets. It finds
useful classification and rubric judgments but warns that binary probabilities
can rank examples well while performing poorly at a fixed 0.5 threshold. Training
split thresholds improve several tasks. Prompts, reference-model inference and
possible benchmark contamination limit comparison; the study does not evaluate
repository retrieval. We therefore rank scores without treating confidence as
truth, automatic acceptance, or a universal threshold. [Study and limitations](https://arxiv.org/html/2609.37647v1).

## Alternatives investigated

These models solve related focused tasks; they do not share one interchangeable
output contract. Only MiniLM was run locally in this experiment.

| Model | Mechanism and practical constraints | Repo Graph decision |
|---|---|---|
| [MiniLM L6 cross-encoder](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2), [ONNX conversion](https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2) | 22.7M parameter query/passage relevance model; Apache 2.0; existing FastEmbed supports CPU ONNX inference | Implemented using the already installed semantic extra; no new dependency |
| [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) | Instruction-aware relevance scorer; 32k context, multilingual, Apache 2.0; larger runtime than MiniLM | Candidate for a later code-focused comparison, without changing deployed serving |
| [Jina reranker v3.5](https://huggingface.co/jinaai/jina-reranker-v3.5) | 0.6B listwise ranking with long context; model card requires custom model code and declares CC BY-NC 4.0 | Investigated; no default integration or redistributed weights |
| [GLiNER2](https://github.com/fastino-ai/GLiNER2) | Schema-driven spans, entities, classification and relations; project is Apache 2.0 | Better candidate for evidence-linked diagram tags than for passage ranking; separate checkpoint/runtime review needed |

MiniLM's published speed table uses a GPU. Our CPU measurements below are the
relevant local latency evidence. Larger context windows do not remove the need
for source filtering or a readable diagram.

## Implementation and bounds

Retrieval remains SQLite FTS5 plus blocked cosine search with reciprocal-rank
fusion. Optional reranking sees the first 32 fused candidates, using the same
retrieval depth as the baseline. Each offered path is bounded to 256 UTF-8 bytes
and evidence to 900 bytes. Jev receives one query state and one self-contained
Score question per candidate in one request. The four fixed levels range from
no evidence to direct implementation/documentation. It never reads more files,
adds a path, creates an import, or changes inventory.

The client rejects requests over 48 KiB and responses over 256 KiB, refuses
redirects, uses a ten-second timeout and does not retry. Model, IDs, legend,
probabilities, score, confidence and usage are validated. Errors retain local
ordering and report `fallback`; attempted requests are distinguished from
successful calls, and unavailable token usage is not invented.

The exact request hash includes the model, query, evidence, instructions and
rubric. Valid responses are cached in the existing SQLite metadata table, capped
at 512 entries. Query text, evidence and credentials are not copied into that
cache; the index itself already contains local evidence. Identical requests use
zero API calls. Changed evidence, query or model invalidates the cache naturally.

The CLI requires `--rerank jev`. The browser requires `serve --allow-jev` plus
selection of Jev for the individual search; it explains the export and shows
used/cached/fallback status. MiniLM is a separate local choice, loaded from cached
weights; downloading it is an explicit `index --reranker` setup step. No
query-time download, hidden provider substitution or GPU allocation occurs.

Source text remains untrusted. TypeSafe documents distracting context,
adversarial text, indirection and Choice order bias. Precise prompts and small
inputs reduce exposure but do not provide a security boundary. Local validation
and the absence of model-driven actions enforce that boundary.
[Known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

## Measured retrieval results

Public snapshots: Terraform AWS `82532de7103d4dbabe384749cfaf09fc4c0692ad`
(20,370 files) and Kubernetes `35fc3af13807e70534fb11736bcccc013631efde`
(25,788 files). Both reuse the complete 0.4.0 BGE-small index. All queries use
limit 10; hit@5 means an expected source path appears in the first five results.
Judgments were frozen before Jev/local scoring and were not generated by either
reranker. Each method receives the same candidate pool. Query hashes, source
commits, returned paths, misses, usage and cache repeats are retained in the
[AWS report](../evaluations/results/jev-aws.json) and
[Kubernetes report](../evaluations/results/jev-kubernetes.json).

| Set (8 queries each) | Hybrid hit@5 | Local MiniLM hit@5 | Jev hit@5 | Shortlist contains expected path |
|---|---:|---:|---:|---:|
| AWS development | 62.5% | 62.5% | 75.0% | 100.0% |
| Kubernetes development | 37.5% | 50.0% | 37.5% | 62.5% |
| AWS fresh | 62.5% | 75.0% | 87.5% | 87.5% |
| Kubernetes fresh | 62.5% | 62.5% | 87.5% | 87.5% |

Across 32 queries, hybrid hits 18/32, MiniLM 20/32 and Jev 23/32. On the 16 fresh
queries, Jev improves from 10/16 to 14/16. It does not dominate every metric:
AWS fresh MRR@10 falls from 0.625 to 0.525 even while hit@5 improves. On old
Kubernetes queries Jev pushes the replica-management expected path outside the
first ten. Source prefixes are incomplete relevance judgments: adjacent code and
tests can be useful, and the old area-based labels can count metadata files.
These small developer-authored samples are not a blinded independent holdout
and establish neither statistical significance nor universal calibration.

Warm uncached end-to-end p50 was 230–252 ms for Jev, 652–731 ms for MiniLM and
56–65 ms for hybrid, including local retrieval and response validation. Eight
observations per set give only a rough p95. Model load is recorded separately;
these are CPU/service observations, not throughput guarantees. Thirty-two live
requests used 350,445 input tokens, with no fallback, for an estimated **$0.01472**
at the dated published rate. Every immediate repeat retained paths and made
zero API calls. Download/setup, cold CLI latency, developer time and human
reading savings are not included in that API price.

The [synthetic probes](../evaluations/results/jev-probes.json) reversed eight
candidates and compared two individual judgments with batched judgments. Maximum
order score difference was 0.10 rubric levels; the direct example differed by
0.04 between single and batch. Two injected negative examples stayed below
partial relevance. Four calls cost an estimated $0.000156. This is a small
diagnostic, not adversarial robustness or probability calibration proof.

Browser checks on both large maps exercise reranker selection, all seven views,
bounded nodes, downloads, source navigation, selection restoration and an 800 px
viewport. [AWS Jev workflow](../evaluations/jev-aws-ux.json),
[Kubernetes local workflow](../evaluations/jev-kubernetes-ux.json). Automated
workflows do not establish that people find or understand answers faster.

## Scale priorities and recommendation

### Frozen function retrieval comparison

The T047 gate compares existing file synopsis and captured function keyword
evidence on four original synthetic files, using eight questions reviewed by an
independent Astra AI source reviewer. Run the supported command:

```sh
uv run python evaluations/acceptance.py --gate task-preflight --task T047 --checks ranking-boundary
```

The [frozen inputs](../evaluations/code-understanding/function-relevance-inputs.json)
and [source review](../evaluations/code-understanding/function-relevance-review.json)
are checked against their committed bytes before extraction. Both arms use the
same source inventory and captured output with no embedder or reranker. File
presence is a proxy; function recall counts physical member IDs over twelve
question/function pairs. Evidence coverage separately requires the reviewed
declaration and supporting body statements. Overlapping parent/child members
share one context family, and unjudged functions are excluded from precision
claims.

The [business report](../evaluations/results/code-understanding/business.json)
retains every actual response, exact-identifier check, hit, miss and source
identity. File search does not expose the function arm's byte/entity/deadline
receipt, so those file fields are unavailable. Equal top ten limits count
different units and do not establish equal work or latency budgets. CPU timings
exclude child processes. A reported process memory high-water covers its whole
lifetime; benchmark peak RSS remains unmeasured. Byte
caps do not establish token savings. This small comparison does not qualify
model quality, large corpora, agent performance or human comprehension.

The corrected execution on 2026-10-08 used Linux x86_64 and Python 3.12.14.
All fifteen physical source checks and four exact-identifier checks passed.
The measured outcomes were:

| Native keyword arm | Retrieved relevant pairs | Complete reviewed statement coverage |
|---|---|---|
| File synopsis | 10/12 file-presence proxy | 0/12 |
| Captured functions | 11/12 physical members | 11/12 |

The body-only `tombstone` query retrieved its function while file synopsis
missed it. Both arms missed the deliberately lexical-absent `reverse remittance`
control. The function diversity query covered all four language/context
families; the nested query retained two relevant IDs in one context family.
No function response truncated or exhausted its deadline, and no inference or
credential-reader guard was invoked. These observations retain the misses and
different metric units; they do not establish full retrieval precision or a
semantic-model benefit. An initial evaluator setup attempt failed before
extraction and is retained separately from the corrected observation.

Richer evidence, package routing, diversification, cached models and ANN remain
deferred until measured limitations justify a separately reviewed comparison.

Ship both choices as opt-in, retaining the zero-API default. Jev is useful for
this measured shortlist ranking task at low API cost, but the older Kubernetes
failures prevent making it the universal default. MiniLM offers a measured local
alternative with higher CPU latency and smaller improvements here.

The next recall experiment should improve evidence extraction and candidate
diversity: names from later declarations, purpose-bearing documentation sections,
separation of metadata/fixtures from implementation, and package-level candidates
followed by file retrieval. Freeze new judgments before selecting that design.
For monorepos, partition by package and reuse content-hashed summaries/vectors;
batch changed documents, not the whole repository into model context. An optional
Jev beam router could select two or three real package prefixes and merge their
results with global retrieval. Require improved candidate recall without losing
exact-identifier results before shipping it.

Do not add a larger generative model or rewrite the language yet. The existing
CPU exact scan meets the measured latency budget on these two corpora and a
synthetic 100k scan; none of this qualifies a million-file monorepo. Introduce an
approximate index only when real corpus scan latency exceeds budget, measuring
recall loss alongside speed. Human task-success/time and source-level relevance
judgments remain the missing acceptance evidence for broader promotion.
