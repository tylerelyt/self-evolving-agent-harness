# Architecture

Self-Evolving Agent Harness performs recurring work, preserves execution evidence, learns scoped facts and reusable methods, evaluates proposed changes, and assigns approved versions to subsequent tasks. Personal and dedicated deployments use the same lifecycle contracts.

![Harness RSI target architecture](../../assets/images/maintained-rsi-system.png)

The diagram includes implemented single-host components and planned distributed, rollout and deeper-evolution capabilities. [Editable source](../../assets/images/maintained-rsi-system.svg).

## Documentation

- [Target architecture](TARGET-ARCHITECTURE.md): responsibilities, execution and learning loops, state ownership and recovery.
- [Shared contracts](CONTRACTS.md): object vocabulary and invariants for execution, evidence, evaluation and adoption.
- [Implementation](IMPLEMENTATION.md): maintained modules, supported behavior and verification limits.
- [Roadmap](ROADMAP.md): remaining capabilities and their acceptance conditions.
- [OpenViking deployment](../../deploy/OPENVIKING.md): installation, supervised lifecycle, native retrieval and encoder limits.
- [Runtime and exercise modules](../adr/0001-system-and-tutorials.md): dependencies and compatibility.
- [Deployment profiles](../adr/0002-personal-and-dedicated-harness.md): personal and dedicated state ownership.
- [Contributing](../../CONTRIBUTING.md): validation, contracts and releases.

## Runtime modules

[`apps/oncall`](../../apps/oncall/README.md) provides continuous inspection, incident handling, the workbench and workload integrations. The installable package includes the application, static resources and shared core. Exercise entrypoints and compatibility wrappers call the maintained modules with their own configuration and state.

The shared core supplies immutable Skill artifacts and bundles, paired evaluation gates, candidate history, durable jobs, personal assignments, signed approvals and action reconciliation. The dedicated deployment resumes the same workspace, native Sessions/Items and approved methods after restart.

`KnowledgeStore` owns exact tenant/environment/service scopes, source-linked revisions, CAS heads, TTL/revocation and a publication outbox. A separate worker reviews completed incidents and publishes facts and runbooks to OpenViking. Reads use the pinned Hermes native provider; local SQLite validates revision authority and delivery state. Runbooks must match the active approved Skill digest. The Memory view exposes citations and publication status.

The default dedicated profile supervises OpenViking 0.4.12 after installation. DeepSeek generates indexing summaries under a cumulative default budget of 60 requests. The supplied character n-gram encoder supports lexical retrieval; semantic ranking quality requires evaluation with the chosen production encoder.

[Personal governed tasks](../../deploy/PERSONAL-LEARNING.md) use evaluated Skill assignments. [Dedicated recovery](../../apps/oncall/ENTERPRISE.md) uses authenticated scope grants, signed approvals and tool receipts. Organization SSO, real service tools and domain evaluation are deployment inputs.

## Supported scope

The current deployment is single-host. Scripted-model checks verify deterministic behavior and protocol integration; real-model runs cover bounded tasks and can reject proposed updates. These results do not establish long-duration quality gains or semantic retrieval quality. Multi-host scheduling, production canaries and autonomous Harness/improver modification remain on the [roadmap](ROADMAP.md).
