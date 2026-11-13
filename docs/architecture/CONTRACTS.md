# Shared contracts

Status: proposed common object and operation vocabulary. Supported persisted formats and interfaces are listed in [implementation](IMPLEMENTATION.md).

## Identity, scope and version conventions

All persisted objects have `schema_version`, a stable object ID, UTC creation time and scope. Scope carries tenant, project, environment and service where applicable; absent scope is not a wildcard. Authorization is checked on every operation independently of identifiers.

An `incident_id` identifies a business incident; `task_id` identifies logical work; `attempt_id` identifies an execution attempt; `session_id` and `item_id` locate Harness context and evidence. A task may have several attempts and Sessions; these identities are not interchangeable. The adapter records mappings to upstream identities.

Content digests identify immutable bytes and their manifest. They do not confer authorization or provide confidentiality. Mutable records use an explicit state version for conditional updates. Object references include enough namespace and scope for authorization.

## Objects

| Object | Required content | Principal invariant |
| --- | --- | --- |
| Event | ID, source, event kind, scope, observation time, payload reference, deduplication key | Duplicate delivery does not create duplicate business work |
| Task | ID, kind, scope, goal, status, state version, incident reference, current attempt | Logical work survives owner replacement |
| Attempt | Task, worker, ownership epoch, checkpoint, execution manifest | Only a current owner may commit new work |
| Checkpoint | Task/attempt, event cursor, upstream Session/Item, pending action references | Resume cannot skip unresolved side effects |
| Action | ID, task, authorization, parameter digest, idempotency key, outcome state and receipts | Unknown outcome is not treated as safe failure |
| Episode | Related tasks/Sessions/Items, original evidence, independent outcomes, feedback and input manifest | Later evidence retains its original availability time |
| FactRevision | Key, content, source, scope, validity, predecessor and verification | An update cannot silently widen sharing scope |
| Candidate | Kind, immutable artifact, parents, Episodes, generator, scope, compatibility, requested rights | A candidate does not directly change the formal version |
| DatasetVersion | Grouping/splits, task specifications, world snapshots, protected ground-truth references | Related incidents cannot leak across intended boundaries |
| EvaluationReport | Candidate/baseline, dataset/world/scorer/model, budgets, per-case results, evidence and validity | Invalid or missing cases remain visible |
| Release | Approved artifact, report, policy decision, assignments, rollback target, operation state | Approval binds exact content and authorized use |
| Adoption | Release, worker, task/round, actual loaded digest, outcome and behavioral evidence | Synchronization alone is not adoption |

## Execution manifest

An attempt records the actual model/provider configuration, Harness revision, tool-adapter revision, policy version, Skill bundle digests and the Memory view used. Every business observation has its own timestamp.

Secrets are referenced through an authorized credential binding; they are not serialized into manifests, prompts or evidence exports. When a provider cannot supply an immutable model revision, the manifest states that limit rather than claiming exact model reproducibility.

## Capability artifact

The artifact manifest lists the Skill body, companion scripts, reference files and declared runtime dependencies by digest. A release approves the complete manifest. Mutable external sources are queried as evidence with freshness requirements, not implicitly bundled into an immutable method version.

Candidates have kinds `memory`, `skill`, `harness` or `improver`. Compatibility includes contract versions, required adapter/runtime capabilities, applicable task types and permission deltas. Requested rights do not grant rights; the release decision evaluates them separately.

## Command semantics

The names below describe logical operations, not shipped HTTP endpoints:

| Operation | Inputs | Receipt |
| --- | --- | --- |
| Submit work/event | Scoped input, idempotency key | Authorized task/event identity and acceptance state |
| Steer / interrupt | Task, command ID, expected state version | Acceptance followed by consumption and final handling states |
| Claim / renew attempt | Worker, task, ownership token | Attempt and monotonic epoch |
| Commit checkpoint | Epoch, expected task version, evidence references | New task version and event cursor |
| Propose candidate | Artifact, sources, generator, scope | Registered candidate identity |
| Evaluate | Candidate/baseline and frozen evaluation specification | Job identity, then immutable report |
| Approve / publish | Exact artifact, report, assignment and decision | Idempotent release operation with phase receipts |
| Record adoption | Worker/task, actual digest and evidence | Verified or incomplete adoption state |
| Roll back | Release, approved target and affected scope | Resumed operation plus fresh adoption verification |

Updates use compare-and-set behavior where concurrent writes can conflict. Side-effect requests are durably recorded before dispatch; a persistent outbox or equivalent prevents committed intent from being lost. Recovery queries uncertain outcomes before replay.

## Evaluation validity

Case results distinguish `passed`, `failed`, `invalid`, `insufficient_evidence`, `budget_exhausted` and `not_run`. A report gives the original denominator and every exclusion. A release gate checks completeness and policy in addition to any aggregate score.

Evaluation workers may expose development feedback to generators. Access to selection reports and final acceptance evidence is separately controlled. Scorers and truth revisions cannot be edited by the candidate being judged.

## Lifecycle and migrations

Persisted schemas are versioned independently of system releases. Additive reader compatibility is explicit; incompatible changes have a migration and a rollback/restore plan. Original receipts and source identities remain addressable through migration mappings.

Adapters map upstream Hermes/SkillClaw representations to these contracts. Contract checks cover round trips, error states, authorization, retries and persisted-format compatibility.
