# Repo Graph contributor instructions

Keep tracked content public and portable. Never include credentials, private
source corpora, generated private diagrams or machine-specific paths. Preserve
unrelated work. Runtime diagram and keyword workflows use only Python stdlib;
optional dependencies need a measured benefit and a documented boundary.

Update the existing skill with the installed skill-creator workflow. The Codex
manifest, Claude manifest and Pi resource list describe the same skill. Preserve original map
entrypoints. New interface behavior needs user documentation and an observable
regression check. Run Python tests, Node viewer tests, browser UX tests, skill
validation and `git diff --check` before release. Search/model changes also run
the frozen large-repository evaluations and retain individual failures.

Use pinned source revisions and record benchmark corpus revisions, model
identity and environment. Never claim model-generated assessments as independent
UX evidence. No source corpus or model weights are shipped in release assets.

This repository is the canonical product. Consumers pin its released source;
do not recreate runtime copies in the marketplace or Anvil bundle. Record
architecture choices in `docs/adr/`, distinguishing accepted implementations
from proposed analysis features. Native harness tests use isolated homes and
make no provider requests or changes to live installations.
