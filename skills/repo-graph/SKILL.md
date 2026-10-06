---
name: repo-graph
description: Map local or public repositories into interactive system diagrams, then search their generated data by meaning or identifiers. Supports large codebases with incremental indexes and bounded results.
---

# Repo Graph

Resolve `../../scripts/repo_graph.py` relative to this `SKILL.md` to an absolute path. Keep the caller's repository as the working directory. Use `python3 "<script>" map [repo-path-or-public-https-url]`; omitted input maps the caller's current directory. Quote shell arguments. Pi invocation is `/skill:repo-graph`; Codex invocation is `$repo-graph`.

Mapping needs Python 3.10+ and no Python dependencies. Git is required for HTTPS inputs. It writes diagrams, Mermaid, JSON and a keyword SQLite index outside the source, under the user's cache by default. `--output DIR` overrides that location; `--refresh` updates a cached remote clone. Report measured file/import/index/cache counts and link the HTML. The System, Explore and Data tabs keep visual detail bounded; the Search tab works through the optional local viewer.

For keyword search, use `python3 "<script>" search "<output>" "<query>" --mode keyword --limit 5`. For meaning search, resolve the plugin root two directories above this skill directory and use `uv run --project "<plugin-root>" --extra semantic repo-graph index "<output>" --semantic` once, then `uv run --project "<plugin-root>" --extra semantic repo-graph search "<output>" "<query>" --limit 5`. The committed lock installs the optional local CPU embedding dependencies; first indexing downloads the model. Subsequent queries use cached models only. If uv is unavailable, follow the package README's pip installation. Never relabel keyword results as semantic.

`--prefix DIR` restricts search to a source area; `--mode keyword|semantic|hybrid` selects retrieval. Results contain source paths, bounded evidence with line references, rank scores and timing. Semantic search is experimental: the published 0.4.0 large-repository benchmark missed its relevance target. Rank scores and vector similarity are not correctness probabilities. Read the relevant source before asserting behavior. Do not load the full graph, corpus or source tree into model context.

Use `repo-graph serve "<output>"` from the installed semantic environment when an interactive search view is wanted. Open the printed loopback URL; search results jump to their file in the diagram. The standalone HTML retains diagrams without a server. Re-map after source changes, then repeat embedding to update only changed documents. Missing or stale semantic indexes cause an actionable error.

The scanner reads at most 64 KiB per supported file. Search indexes every inventoried file path and adds a bounded synopsis for supported text, not full-code chunks: later declarations may be omitted. Go/Python/JS/TS imports are heuristic; other languages appear structurally and in search. System groups describe source areas, not verified services. Mermaid covers the first root page. All paths and observed import links remain in JSON.

Local indexing/search sends no repository data to a provider. Generated data can contain sensitive paths and evidence; review before sharing. Model setup only downloads public weights. `--jev` is an independent explicit opt-in that sends up to 16 top-level directory names for advisory labels. It uses only `TYPESAFE_API_KEY` from the environment or its exact entry in `~/.env`; obey project credential-access rules before invoking it, never source or display that file. Labels cannot create imports or change inventory.
