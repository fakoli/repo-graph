# ADR 0002: Native harness installation

- Status: Accepted
- Date: 2026-10-06

## Context

Users need a short way to enable Repo Graph in Pi, Codex or Claude Code. Each
harness already owns plugin discovery, installation and removal. Editing provider
configuration or creating an extension framework would add another state owner.

## Decision

Provide `repo-graph init --harness pi|codex|claude|all`. Use the installed harness's
native package/plugin manager with a reviewed canonical release. The existing
skill and relative script paths serve every harness. Python mapping stays
dependency-free; optional semantic setup remains an explicit indexing step.

Preview installation commands before applying them when requested. Detect
missing CLIs before starting a multi-harness installation, report failed commands
and preserve successful installations. Native managers own configuration,
idempotence and removal. Do not rewrite credentials, model selection or unrelated
settings. Verify installation and skill discovery using isolated homes.

Existing Claude installations use native `plugin update` so version changes
take effect. Codex/Claude verify the registered source/ref and cached product
version/content before success. The dry-run plan leaves this conditional update
and read-only verification to the applied run. Failed verification reports an
error instead of claiming the requested product is active.
Codex requires native marketplace removal/re-addition for a different source/ref.
Check the requested product before that transition and reject unrelated sources
using the same marketplace name. Preserve other marketplace registrations.

## Consequences

No daemon, model call, hook or custom tool is required for harness support.
The installed CLI must work from a Python wheel as well as a source checkout.
Harness versions and native installation results accompany release evidence.

The command is implemented in 0.6.0. A built-wheel CLI check and isolated native
Pi, Codex and Claude install/discovery checks pass without provider calls.
Pi/Claude project scope is supported; Codex/all project scope is rejected before
installation. The selected CLIs must all be present, including in dry-run mode.
`--source LOCAL_PRODUCT` tests an existing checkout; `--ref TAG` selects a remote
revision explicitly. These controls do not change the analysis pipeline.
