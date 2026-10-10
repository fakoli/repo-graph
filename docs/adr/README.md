# Architecture decisions

Repo Graph is one product for human codebase exploration and bounded agent
retrieval. Accepted decisions describe shipped behavior. Proposed decisions
remain design work until their evaluation gates pass.

| ADR | Status | Decision |
| --- | --- | --- |
| [0001](0001-canonical-product.md) | Accepted | One canonical repository and release |
| [0002](0002-native-harness-installation.md) | Accepted | Native installation for Pi, Codex and Claude |
| [0003](0003-polyglot-fact-index.md) | Proposed | Shared incremental polyglot fact index |
| [0007](0007-bounded-review-workflow.md) | Proposed | Bounded Repo Graph reviews, evidence and native workers in Codex |

Update a decision when evidence changes. Superseding it requires a new ADR that
links back to the old one. Do not silently turn a proposal into an implementation
claim. Research and benchmark requirements live in
[polyglot feasibility](../polyglot-feasibility.md).
