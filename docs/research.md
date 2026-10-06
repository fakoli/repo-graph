# Search and monorepo design

The baseline scanner already mapped a 20k-file repository with bounded reads
and no generative model calls. Keep Python for orchestration: SQLite and ONNX
perform the expensive operations in native code. A Rust rewrite would need to
prove an indexing or query improvement before its maintenance cost is justified.

| Technique | Decision | Evidence and ceiling |
|---|---|---|
| SQLite FTS5/BM25 | Implement for exact names and natural-language keywords | [SQLite's FTS5 docs](https://www.sqlite.org/fts5.html) describe indexed retrieval and BM25 ranking. Split camelCase and underscores before indexing; use SQLite Porter stemming and exclude common query stopwords. |
| Local dense embeddings | Implement as an optional dependency | [FastEmbed](https://github.com/qdrant/fastembed) uses ONNX inference and exposes passage/query encoding. CPU-only BGE-small is a 384-dimensional MIT model in the [supported model catalog](https://qdrant.github.io/fastembed/examples/Supported_Models/). No embedding service or GPU allocation is required. |
| Hybrid rank fusion | Implement equal reciprocal-rank fusion, constant 60 | [Cormack et al., SIGIR 2009](https://doi.org/10.1145/1571941.1572114) motivate combining retrieval ranks rather than comparing incompatible raw scores. Validate against keyword-only and vector-only baselines; fusion can hurt individual queries. |
| Exact vector retrieval in blocks | Implement; fetch 512 vectors at a time | [Sentence Transformers](https://sbert.net/examples/sentence_transformer/applications/semantic-search/README.html) documents corpus/query embeddings and cosine retrieval. Blocks bound transient vector memory; database I/O remains linear in indexed files. |
| HNSW / approximate search | Upgrade if measured query latency exceeds the one-second warm budget | [Malkov and Yashunin](https://arxiv.org/abs/1603.09320) describe hierarchical approximate search. It adds index maintenance, RAM and recall tradeoffs. Exact retrieval is the correctness baseline for that comparison. |
| Package/source-area sharding | Use prefix filtering now; shard independently when corpus size demands it | The diagram already partitions source areas. A prefix is a real directory boundary, not an arbitrary substring. Separate shard updates can bound rebuild work; cross-shard search must merge ranks. |
| File synopses / incremental persistence | Implement one synopsis per inventoried file, with evidence for supported text | Hash the extracted evidence; unchanged mtime/size skips reads, unchanged hashes preserve vectors, changed evidence clears vectors, deleted files disappear. Batch commits retain completed embeddings if interrupted. This is file-level discovery, with a known 64 KiB read and synopsis ceiling. |
| AST chunks / learned reranker | Next relevance experiment after this baseline | The measured baseline misses show a need for richer evidence and retrieval diversity. Language-specific ASTs improve symbol boundaries but expand dependencies and vector count. A reranker adds per-query inference. Evaluate either against this corpus and a new held-out query set before adopting it. |

## Evaluations

`evaluations/queries.json` contains source-area judgments written before scoring.
There are eight queries each for the Terraform AWS provider and Kubernetes:
six descriptions and two identifiers. Expected prefixes are reviewed against
source paths. They describe acceptable relevant areas, not exhaustive relevance
judgments. Keyword results informed stemming/stopword and Markdown-support
fixes; these are development benchmarks, not an untouched independent holdout. No model grades its own answers.

`evaluations/run.py` compares keyword, semantic and hybrid retrieval with hit
rate at 5 and mean reciprocal rank at 10. It records the exact repository commit,
judgment-file hash, individual returned paths, index/output sizes and warm
median/95th-percentile query latency. The acceptance gate is hybrid hit rate at
5 >= 75% per repository, warm p95 < 1 second, complete embedding coverage,
complete System file accounting and <= 12 System nodes. Query timings include
embedding each query but exclude initial model load. CLI cold time is measured
separately in the report. Small judgment sets do not establish universal code
search quality; retain individual misses.

`tests/ux.mjs` drives the real browser through every view, search, source jump,
query restoration and a narrow viewport. It checks inventory, visible-node
bounds, load time and browser errors. Screenshots are inspection evidence.
This is automated UX regression coverage, not a study of human task completion.

Large public fixtures establish real file-count scale and integration behavior.
They do not prove million-file performance. For that scale, measure rebuild,
peak RSS, query p95 and disk usage first; test sharding/ANN recall against exact
search before switching defaults. Model/corpus changes require new evaluations.
