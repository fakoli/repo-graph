# Repo Graph

Local system diagrams and semantic search for large repositories, packaged for
Pi, Codex and Claude Code. Map a repository without sending it to a model;
search its generated index by meaning or exact identifiers and jump from a
result to its file in the diagram.

## Install and use

```bash
uv tool install 'repo-graph-agent[semantic] @ git+https://github.com/fakoli/repo-graph@v0.6.0'
repo-graph map /path/to/repository
```

Use the output directory printed by the scan:

```bash
repo-graph index OUTPUT --semantic
repo-graph search OUTPUT 'where are access permissions checked?' --limit 5
repo-graph serve OUTPUT
```

The viewer prints a loopback URL. Search results open their source file in the
diagram. The generated HTML also works directly from disk for diagrams.
`repo-graph map https://github.com/owner/repository` clones a public repository
into the user cache; `--refresh` updates it. An omitted path maps the caller's
current directory. `--output DIR` must be outside the source repository.

Mapping and keyword search need Python 3.10+ and no Python dependencies. Git
is required for HTTPS input. The optional semantic extra uses pinned FastEmbed
and NumPy for batched ONNX CPU inference and vector scoring. First embedding
index creation downloads the public BGE-small model; queries and the viewer
use cached model files only. No GPU or embedding service is required. To install
without uv, use `python3 -m pip install 'repo-graph-agent[semantic] @ git+https://github.com/fakoli/repo-graph@v0.6.0'`.

## Coding agent plugins

After installing the CLI, enable a harness with one command:

```bash
repo-graph init --harness pi
```

Use `--harness codex`, `--harness claude` or `--harness all` to select other
installed harnesses. `--dry-run` previews the native commands. Installation uses
the reviewed canonical release through the harness's own package manager.
All selected CLIs must be present before installation starts. User scope is the
default; `--scope project` works for Pi and Claude. Codex supports user scope.
The command reports completed harnesses if a later install fails; native changes
may remain, and retrying the same command is supported. Start a fresh session
after installation. Development uses `--source LOCAL_PRODUCT`; `--ref TAG`
overrides the remote release pin.

| Harness | Invocation |
| --- | --- |
| Pi | `/skill:repo-graph` |
| Codex | `$repo-graph` |
| Claude Code | `/repo-graph:repo-graph` |

Every harness loads the same skill, scanner, search engine and viewer. Packaged
scripts resolve from the skill's location while the caller's repository remains
the working directory. Mapping uses ordinary read/bash permissions. Optional
semantic dependencies use `uv run --project … --extra semantic`; the scanner
and keyword search need no Python dependencies. The agent's selected model still
handles instructions.

For direct native installation:

```bash
pi install git:github.com/fakoli/repo-graph@v0.6.0
codex plugin marketplace add fakoli/repo-graph --ref v0.6.0
codex plugin add repo-graph@repo-graph
claude plugin marketplace add https://github.com/fakoli/repo-graph.git#v0.6.0
claude plugin install repo-graph@repo-graph
```

The existing `fakoli/agent-plugins` marketplace also lists this pinned canonical
product. Anvil Extensions includes a compatibility package that depends on the
same release; it carries no copied scanner, index, skill or viewer. Update those
consumers through their reviewed releases. Uninstall with the native manager.

## Product architecture

Repo Graph has one canonical source repository. Human diagrams and agent search
share its generated data. [Architecture decisions](docs/adr/README.md) record
repository ownership and harness installation, plus the proposed next step:
a shared incremental fact index for symbols, references and candidate calls.

[Polyglot feasibility and research](docs/polyglot-feasibility.md) distinguish
syntax coverage from semantic resolution, compare reusable engines, and define
Odoo/Django and cross-language evaluation gates. A language-independent fact
schema is feasible; each language and framework still needs resolution rules.
Version 0.6.0 does **not** add function call graphs. Existing diagrams show
source structure and heuristic imports.

## Views and search

System groups up to 12 source areas and their observed imports. Explore offers
card, tree, radial and file-count treemap layouts. Data includes a table and
directed dependency matrix; export filtered scope data as CSV or the viewport
as SVG. The Search tab queries the whole indexed corpus through the opt-in
loopback viewer. It preserves the query, method, reranker and path prefix when
returning from a diagram. Use **Path prefix** to restrict results to a source
area. Click a breadcrumb or **Root** to move through the map. Selected files are
centered, focused and named in the inspector; Escape closes details. Focus the
canvas to pan with arrows, zoom with +/− or fit with F. Tabs also work with arrow
keys. Narrow layouts stack controls and retain full component navigation.

### Optional reranking

Jev can judge a shortlist in one batched request. It is disabled by default:

```bash
repo-graph search OUTPUT 'record API activity in an audit trail' --rerank jev
repo-graph serve OUTPUT --allow-jev
```

The browser also requires selecting **Jev API reranker** for the search. It
exports the query and up to 32 paths with 900 bytes of evidence per file, under
a 48 KiB request cap. Identical requests are cached; failures retain local
order and show `fallback`. JSON receipts record model, successful API calls,
attempts, usage and timing. Jev cannot recover files missing from the shortlist.

For the CPU alternative, using the existing semantic extra:

```bash
repo-graph index OUTPUT --reranker
repo-graph search OUTPUT 'record API activity in an audit trail' --rerank local
repo-graph serve OUTPUT --local-reranker
```

Setup downloads MiniLM once; searches use cached weights. Both rerankers are
optional. On 16 newly frozen public-repository queries, Jev improved hit@5 from
10/16 to 14/16. On the old Kubernetes set it did not improve hit@5, and some
individual rankings worsened. See the [research and measured comparison](docs/jev-research.md).

Semantic search is experimental in 0.4.0. Hybrid retrieval found an expected
source area in the top five for 5/8 AWS and 3/8 Kubernetes benchmark queries,
below the 75% relevance target. Inspect evidence and use identifiers or a source
prefix for precision. The diagrams, indexing and browser workflow checks passed;
these checks do not establish search relevance.

Search modes are `hybrid` (default), `semantic` and `keyword`:

```bash
repo-graph search OUTPUT 'cache invalidation' --prefix packages/cache --mode hybrid --limit 5
```

JSON results include paths, bounded evidence with line references, rank scores,
similarity and timing. These scores are ranking signals, not proof of correctness.
The CLI caps results at 50 and queries at 1,000 characters. Semantic search requires
a complete index; missing/stale vectors cause an actionable error. Re-map after
editing source, then repeat `index --semantic`: unchanged summaries reuse their
vectors, changed summaries are re-embedded, and deleted files leave the index.
Indexing commits batches so interrupted work can resume. Keyword search works
immediately after mapping.

## Scale and limits

The scanner processes 512-file batches and reads at most 64 KiB per supported
source file, caching Go/Python/JavaScript/TypeScript imports. Other languages
appear in the structural map and supported text languages in search. Every
canvas shows at most 24 nodes and 40 links. Full paths and aggregated observed
imports remain in `graph.json`; Mermaid covers the first root page.

Search stores every inventoried file path, with one bounded synopsis of comments,
declarations and documentation for supported text and its 384-dimensional vector
in SQLite. It does not index every function or
entire files. Late declarations can be omitted. Exact vector retrieval fetches
512 vectors per block, bounding transient vector memory; query I/O remains
linear in the indexed corpus. Full structural inventory is held in memory.
Generated and hidden paths are excluded; untracked, nonignored source is included.
This is source architecture and heuristic imports, not verified runtime services
or call flow. Prefix filtering supports source-area queries. Million-file support
is not claimed. See [research and scale decisions](docs/research.md) and
[measured evaluations](evaluations/README.md). The [experience and performance review](docs/graph-experience.md) records the 0.5.0 design changes and before/after measurements.

## Data and network boundaries

Outputs and shallow clones live under `~/.cache/repo-graph/` by default. Generated
HTML/JSON/SQLite data can reveal private names, source evidence and paths: review
before sharing. The corpus and vectors remain local. Model setup downloads weights
only. The viewer loads no remote resources. Its server binds only
to loopback and rejects foreign Host/Origin and non-JSON search requests; it serves
only the generated HTML, JSON and Mermaid artifacts. Run it only for locally generated output you
trust. Ctrl+C stops it. Repeated maps replace generated files in the output
location; do not store unrelated files there. Sources are never executed. One
model search runs at a time; status, downloads and plain keyword search stay
responsive during inference. Additional model searches get an explicit busy response.

`map --jev` is an independent opt-in. It makes at most one TypeSafe request with
up to 16 top-level directory names for advisory labels, which can incur API cost.
It reads only `TYPESAFE_API_KEY` from the environment or its exact entry in `~/.env`,
without sourcing/logging the file. Project credential access rules still apply.
Jev cannot create import edges. It is not needed for semantic search. The separate
`search --rerank jev` and viewer `--allow-jev` options authorize bounded source
export. All Jev calls pin `jev-1.13.0`, refuse redirects and make no automatic
retries. Cached ranking responses exclude raw queries/evidence and retain at
most 512 entries. Credentials are never stored in the index or browser.

Uninstall the plugin through the host's package manager. Generated artifacts and
caches remain available. Rollback is a prior reviewed release pin; no source state
is changed. To clear local data, delete only the corresponding generated cache
entry. A failed clone can leave an incomplete cache entry; remove that entry and
retry. A failed embedding batch preserves previously committed vectors.

## Development and evaluations

```bash
uv sync --extra semantic --locked
uv run python -m unittest discover -s tests -v
npm ci
npm test
REPO_GRAPH_PYTHON=.venv/bin/python npm run test:ux
python3 tests/pi_smoke.py
python3 tests/harness_smoke.py --harness codex
python3 tests/harness_smoke.py --harness claude
```

UX tests need Chrome/Chromium at `REPO_GRAPH_CHROME`, or the documented default
system Chrome path; this is a test dependency only. Retrieval evaluations and
commands are in [evaluations/README.md](evaluations/README.md). Offline regression
checks and real public-repository benchmarks cover different requirements.
Native smoke checks require the corresponding installed harness CLI, use isolated
homes, and execute mapping/search without provider requests. They do not change
live harness installations. The Codex/Claude checks also repeat initialization
and inspect the installed shared skill. `uv build --wheel` checks the CLI package;
native plugins fetch their skill and runtime from the pinned product source.

MIT. [Provenance](UPSTREAM.md) records the original scanner/skill and this release's
adaptations. No benchmark repositories or model weights are redistributed.
