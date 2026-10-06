# Provenance

Repo Graph originated in the MIT public `fakoli/agent-plugins` repository at
commit `6a4463cb352b265b983994d53d53b9b7a0c0cae0`, plugin version 0.3.0.
This standalone repository becomes the canonical source for version 0.4.0.
The original MIT license and copyright are retained in [LICENSE](LICENSE).

The bounded scanner, system partition and offline view helpers are carried
forward. Adaptations: package-relative assets; corrected unresolved-import
resolution; incremental SQLite file synopses; optional local ONNX embeddings;
exact blocked cosine search and reciprocal-rank fusion; a loopback Search UI;
portable CLI and Pi/Codex skill paths; retrieval, safety and browser evaluations.
These files are maintained here and are not represented as byte-identical to
0.3.0. No source from Graphify or GitDiagram is included.

FastEmbed 0.7.4 is an optional Apache-2.0 library installed from its distribution,
without modification. NumPy and ONNX Runtime retain their library licenses.
BGE-small-en-v1.5 is a separately downloaded MIT model; no weights are vendored.
Playwright 1.63.0 is a development-only browser-testing dependency. Locked
Python and npm dependency metadata accompany the release. Local sources, model
cache, benchmark databases and generated diagrams are excluded from Git.

Version 0.5.0 adds optional bounded Jev shortlist judgments and local MiniLM
reranking, using the same semantic extra. TypeSafe is an opt-in remote API;
no SDK is added. The separately downloaded Xenova ONNX conversion of
MS MARCO MiniLM L6 v2 is Apache-2.0; no weights or benchmark source are vendored.
Research sources, frozen query comparisons and failure cases accompany the change.
