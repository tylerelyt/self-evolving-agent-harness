# Contributing

Start with the [architecture](docs/architecture/README.md), [module boundaries](docs/adr/0001-system-and-tutorials.md) and [roadmap](docs/architecture/ROADMAP.md).

## Code and documentation

- `src/self_evolving_harness/` contains shared contracts, persistence and lifecycle logic.
- `apps/` contains workloads, tool and memory adapters, and the workbench.
- `deploy/` contains installation, supervision and deployment configuration.
- `examples/` and `workshop/` contain independently runnable exercises and compatibility entrypoints.
- `docs/architecture/` describes implemented behavior, constraints and planned extensions.

Runtime dependencies flow from applications and exercise entrypoints toward the shared core. Preserve existing entrypoints and persisted evidence when moving a module. Keep upstream revisions pinned; adapter upgrades include compatibility checks against the new revision.

## Validation

Run the relevant suites for a behavior change:

```bash
python3.12 -B scripts/check_offline.py
node apps/oncall/web/test_ui.cjs
```

Use the pinned runtime environment for native integration checks. Cover the affected public behavior, including retry, interruption, scope and rollback where relevant. Report scripted-model, recorded replay, real-protocol and real-model results separately, with the command and verification mode needed to reproduce them.

For documentation changes, check links, example commands and feature status against the implementation.

## Compatibility and releases

Version persisted schemas separately from system releases. Incompatible object or interface changes require a migration and rollback plan. Release approval binds immutable artifact digests, evaluation results and assignments.

[Exercise compatibility](tutorials/compatibility.json) records full baseline commits and upstream pins. Update a baseline after running the affected exercise against that revision. Each exercise specifies its editable files, input data, expected observations and checks.

## Contributions

Describe the problem, resulting behavior, affected contracts and validation. Keep credentials, runtime databases, private input data and session histories out of commits. Link reproducible checks and public fixtures where possible; distinguish functional verification from task-quality or production-readiness claims.
