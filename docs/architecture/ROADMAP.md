# Engineering roadmap

The current foundation supports single-host continuous On-call execution, scoped external memory, governed Skill publication and personal task assignments. Further work is accepted against observable behavior, quality and recovery criteria.

## Continuous task quality

Extend real-model checks across independently grouped incidents, environments and learning batches. A rejected candidate remains a valid release-control outcome; demonstrating improvement also requires a passing comparison and subsequent task adoption.

Acceptance:

- Inspection, alert routing, investigation, paging and authorized recovery retain original tool and Session/Item evidence.
- Reports bind the model configuration, tools, budgets, independent outcomes and failures.
- Repeated incidents and controlled interruptions preserve ownership and pending actions.
- Published versions are verified in subsequent tasks; rejected candidates leave approved assignments active.

See [implementation](IMPLEMENTATION.md#task-quality-scope) for the current quality evidence and its limits.

## Durable execution and learning

The single-host control store provides immutable jobs/events, leases, epochs, checkpoints, retries and SQLite backup. On-call resume preserves Sessions/Items, incidents, paging, approved methods and environment identity. The action journal reconciles uncertain external effects.

Extend these mechanisms to multi-host ownership and cross-host task recovery.

Acceptance:

- Execution and learning can restart independently after action dispatch but before receipt persistence.
- Duplicate events do not duplicate incident work, paging, recovery or publication.
- Stale owners cannot commit new work, and uncertain effects remain unresolved until reconciled.
- Restore reconstructs a consistent task, evidence and artifact view.

## Retrieval and evaluation quality

Continuous memory uses source-linked revisions, exact scopes, CAS heads, TTL/revocation and an outbox. Native Hermes OpenViking tools perform retrieval; the local authority filters stale revisions and mismatched method versions. The default managed server uses bounded DeepSeek summaries and a local lexical encoder.

Evaluate production encoders and task policies against versioned, incident-grouped datasets with protected truth and separate development, selection and acceptance splits.

Acceptance:

- Source-valid recall, ranking, missing hits, scope leakage, expiry and method-version changes are measured with the selected embedding/index configuration.
- New and old methods run under matched model, Memory, environment and budget conditions.
- Tests cover healthy and intermittent services, incomplete evidence, stale facts and unauthorized recovery requests.
- Missing observations, correlated samples, dataset leakage and scorer revisions remain explicit.
- Retained gains have repeated held-out evidence and uncertainty estimates.

## Governed production rollout

Complete Skill bundles, personal assignments/adoption/rollback and authenticated scoped tool approvals are implemented. Production rollout additionally needs organization-owned identity/tool integration, canary cohorts and predeclared stop conditions.

Acceptance:

- Interrupted publication and rollback resume without duplicating work or reporting incomplete operations as successful.
- Concurrent incidents retain assigned versions, and fresh tasks prove adoption.
- Missing receiver receipts remain visible as partial completion.
- Operational and quality monitoring can independently stop exposure.
- Restore and incident-response drills pass with the deployed tools and authority.

## Harness and improver evolution

Extend candidate kinds to bounded runtime patches and learning/search methods. Use isolated workspaces, pinned starting capabilities, research lineage, matched budgets and independent descendant-quality evaluation.

Acceptance:

- New improvers are compared with the current one using the same starting point, experience and total budget.
- Selection tasks are separated from external transfer checks.
- Runtime compatibility, cancellation, persistence and policy checks pass alongside task quality.
- Candidates cannot alter their final scorer, authorization floor, release gate or protected truth.

## Version compatibility

System releases identify interfaces and behavior. Candidate/artifact digests, persisted-schema versions and exercise revisions are separate identifiers. [Exercise compatibility](../../tutorials/compatibility.json) records full baseline commits and upstream pins; affected entrypoints are checked before updating a baseline.
