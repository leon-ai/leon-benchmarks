# Environment recipes and restoration receipts

The frozen run's `provenance/campaign-snapshots/` is the reference for native
runtime templates, container mounts, published task settings, image IDs,
input restorations and dependency receipts. This directory does not include
container images, source installs, caches or virtual environments.

Relevant records:

- `selection.json` and `wild-metadata.json`: exact tasks, upstream revisions,
  native prompts/configuration and published budgets.
- `runtime.json`, `mounts.json`, `runtime-provenance.json`: native entry points,
  isolated sources, browser/Python aliases and configuration constraints.
- `social-service-image.json`, `social-native-service-source-review.json`,
  `social-original-workspace-source-inventory.json`, `social-service-repair-plan.json`:
  canonical author inputs, original service restoration and dependency/image IDs.
- `safety-fixture-grader-validation.json`: identical immutable-initial-fixture
  controls for the two repaired Safety graders.
- `replacement-environment-retirement-20261012.json`: task-owned retirement receipts.
- `pinch-preparation.json`: preparation state, not passed native session controls.

Rebuild inputs from the pinned upstream datasets and validate their hashes before
scoring. Hash inventories identify historic outputs and runtime bytes but do not
supply those bytes. An image ID is an identity receipt, not a promise that a
registry still serves that image. If the exact image/source/dependency cannot be
recovered, disclose the mismatch and hold comparable scoring.

`__HOME__`, `__SOURCES__`, `__WORKSPACE__`, `__PRIVATE_WORKSPACE__` and
`__PRIVATE_ARCHIVE__` are publication placeholders. Remap host source paths and
native container aliases explicitly; do not treat placeholders as runnable paths.
Prepare each task for the full nine-attempt cohort, keep mutable state isolated,
and retire owned resources only after evidence export and the final consumer.
Never prune unrelated Docker resources.
