# ADR 0001: One canonical Repo Graph product

- Status: Accepted
- Date: 2026-10-06

## Context

The standalone product, agent marketplace and Anvil Pi package carried copies
of the same scanner, search engine and viewer. Fixes could land in one copy
without reaching the others. The public product now needs Pi, Codex and Claude
support with the same data and behavior.

## Decision

`fakoli/repo-graph` owns the runtime, shared skill, harness metadata, evaluations
and ADRs. A single version identifies its Python distribution, Pi package and
Codex/Claude plugin manifests. Harness adapters describe loading and installation;
they contain no scanner or search implementation.

`fakoli/agent-plugins` keeps its existing catalog identity and points its Repo
Graph entry at a reviewed canonical release. Anvil Extensions keeps a thin
compatibility package for existing Pi users, depending on that same release.
Old script entrypoints delegate to the canonical runtime. Neither consumer
maintains a copied runtime, skill or algorithm test suite. Native bundle
integration checks remain with the consumers.

## Consequences

Diagram and search fixes ship once. Consumers choose when to update their
release pin and retain their own review gates. Installation can require fetching
the canonical source; a consumer source archive is no longer a vendored copy
of Repo Graph. Existing standalone map/search commands remain supported.

This consolidation does not add function-level static analysis. Current maps
still show directories, files and heuristic imports.
