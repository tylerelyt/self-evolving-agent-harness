# Implementation and acceptance

The single-host implementation separates execution, memory, learning, evaluation, release and tool authorization. Distributed scheduling and production rollout are planned extensions.

## Maintained code

| Responsibility | Implementation |
| --- | --- |
| Persistent deployment supervision | `src/self_evolving_harness/deployment.py`, `deploy/` |
| Continuous On-call and finite acceptance | `apps/oncall/service.py`, `workflow.py`, `acceptance.py` |
| HTTP world, external monitoring, inspection and investigation | `apps/oncall/patrol/` |
| Business workbench and explicit tutorial mode | `apps/oncall/workbench*.py`, `web/` |
| Runtime integration/scorer resources | `apps/oncall/integrations/` |
| Immutable Skill artifacts and complete bundles | `artifacts.py`, `bundles.py` in the core |
| Independent paired gates and candidate history | `evaluation.py`, `coordinator.py`, `learning.py` |
| Durable job ownership/checkpoints/retries | `control.py` |
| Scoped fact revisions, CAS heads and publication outbox | `knowledge.py` in the core |
| Real OpenViking publication and native Hermes retrieval | `adapters/openviking.py` in the core |
| Automatic incident review, fact/runbook publication and Memory view | `apps/oncall/memory.py`, `workbench_memory.py` |
| Managed OpenViking installation and lifecycle | `deploy/setup_openviking.py`, `deploy/openviking.py`, `deploy/oncall.py` |
| Personal learning, assignments, adoption and rollback | `personal.py`, `deploy/personal.py` |
| Scoped approval and action reconciliation | `authority.py`, `apps/dedicated/`, `apps/oncall/enterprise.py` |

The installable package includes the core, application, integration modules and static resources. Existing `workshop/capstone` entrypoints execute the maintained modules. Exercises have separate inputs, configuration and state.

## Continuous execution and durable learning

The service starts real local HTTP worlds and native Hermes/SkillClaw workers.
External monitoring emits incident IDs; the Harness steers actual model turns,
archives source Sessions/Items, forks investigation, pages an engineer and
continues inspection while approval is pending. Recovery is checked against
fresh, distinct healthy windows.

`serve --resume` retains the same workspace, approved Skill, patrol database,
native Sessions/Items, pending pages and learning state. Model/endpoint,
implementation, patrol scope and environment bindings are checked before new
workers start. The local world retains its environment ID and monitoring intent
across restart, while HTTP measurement windows start afresh. Single-host locks
prevent another effective service or environment owner.

Both continuous learning and finite acceptance use the shared candidate ledger.
Continuous learning additionally enqueues immutable experience batches in the
control store, renews a lease, saves phase checkpoints, fences publication and
uses persistent retry delays and attempt limits. It reconciles interrupted
publication before admitting new work. A queue completion cannot substitute
for the candidate's evaluation, delivery or adoption evidence.

## Continuous OpenViking memory

Memory publication is independent of Skill candidate generation. After an incident closes, the memory worker verifies the original initial/investigation Session and report Items, actual tool receipts and recovery observations, then runs native Hermes fact review. A proposed historical fact receives an immutable revision in `KnowledgeStore`; the recorded incident cannot be silently rewritten. Its approved Skill read can also create a source-linked runbook revision. Runbooks are selected only when their exact Skill digest matches the active approved assignment.

The knowledge authority records exact tenant/environment/service scope, source content hashes, predecessor, expiry and revocation. A compare-and-swap head update preserves older revisions; a durable outbox separately publishes immutable documents into real OpenViking and checks index receipts, exact content readback and actual native search/read visibility before acknowledging delivery. Retry after interruption uses the same revision identity. Expired, revoked, superseded, unacknowledged or mismatched revisions cannot become active facts through stale search hits.

On-demand reads use the pinned Hermes native OpenViking `viking_search` and `viking_read` tools. Returned citations are checked against the published revision and the local authority. A provider outage remains an explicit unavailable result; the application does not substitute a local SQLite recall. Memory remains historical evidence and is followed by current observations when assessing a live incident. The workbench Memory view exposes publication/provider state and verified fact/runbook sources.

The dedicated deployment supervises an OpenViking 0.4.12 service with its own persistent workspace. Install it first with `deploy/setup_openviking.py`; provide private DeepSeek model configuration for indexing summaries before launching the default dedicated profile. The cumulative model budget defaults to 60 requests and persists across restarts. The supplied 512-dimensional character n-gram feature-hashing encoder is query/document dependent but lexical, not a semantic embedding model. Indexing/summary failure and the possibility of OpenViking's summary fallback remain explicit; successful indexing is not summary-quality or semantic-recall certification.

An organization can use an externally managed OpenViking service and its own encoder/model configuration. The bundled loopback trusted server does not implement enterprise remote tenant authorization. [OpenViking deployment](../../deploy/OPENVIKING.md) specifies ownership, configuration, model-budget behavior and native-server checks.

## Personal and dedicated execution

The real personal Hermes Gateway owns a writable learner home. Governed tasks
read separately assigned complete bundles in fresh task homes; native updates
cannot change the formal assignment through the learning API. Independent
operator-owned evaluator commands check actual tools before publication. Next
task adoption is verified against native SessionDB `skill_view` output and the
unchanged complete bundle, not an Agent's claim. Rollback changes future task
assignment and preserves already-running task pins.

This is a trusted single-user boundary, not an OS sandbox. Terminal tools under
the same OS identity retain that identity's authority. Multi-tenant deployments
need distinct service identities or containers and scoped tools.

The dedicated tool gateway implements Bearer-authenticated principals, exact
scope grants, signed time-limited approvals and a durable action journal. A
Workbench approval can use that gateway to clear an actual scoped fault through
an operator-configured tool subprocess. It binds incident, environment, fault
generation, evidence, method version and action parameters. Unknown outcomes
are reconciled against the tool's state without repeating dispatch. Configured
authority failure cannot fall back to local recovery; model children do not
inherit the Workbench's approval credentials.

## Verification modes

- Dependency-free system and tutorial checks remain in
  `scripts/check_offline.py`; native-only checks are explicitly skipped there.
- Native core/domain regressions use the pinned Hermes Python, including
  real multiprocess ownership, abrupt process exit, publication interruption,
  HTTP authorization/tool execution and actual service restart.
- A wheel-only run outside the repository blocks teaching-module imports,
  opens the workbench over HTTP, performs repeated native inspections and
  verifies shutdown of the owned workers.
- Personal real-model acceptance uses DeepSeek and native Hermes for a real
  script repair, fixed four-case tool comparison, publication, a new task's
  actual Skill/tool use and rollback followed by another actual task.
- On-call real-model acceptance uses a bounded, authenticated loopback model
  transport with request/usage accounting. Original failed runs are retained.
  Prompt/schema failures, business rejection and transport/budget failure are
  recorded separately; none is converted into an improvement claim.

Native background reviews intentionally disable ordinary SessionDB persistence.
Their original captured messages, call IDs and review-worker flags are therefore
saved separately, including on rejection, without storing model credentials.
Review tool traces retain call order, successful receipts, source content and the resulting candidate or decision.

These are single-host functional and limited real-model observations. A Skill
read proves assigned-method adoption; it does not prove every later business
result. Token usage is reported when returned by the provider; unknown billing
is not represented as free execution. Longitudinal gains need repeated held-out
runs and independent business observations.

## Task-quality scope

The bounded personal DeepSeek/Hermes check repaired a checker script and passed four fixed tool cases. Subsequent native tasks used the published bundle and, after rollback, the parent bundle. This establishes that integration's observed behavior change; broader task quality requires additional datasets and repeated runs.

The two-batch DeepSeek On-call check completed incident handling, peer Memory recall and diagnostic Memory revision. Both proposed On-call revisions and the auxiliary recovery Skill were rejected, leaving the baseline active. Real-model On-call quality improvement and learned-version adoption were therefore not established by that check. The positive collective-learning publication/adoption scenario used scripted task-model decisions with native runtime and HTTP integration.

OpenViking checks cover exact-source publication, native retrieval, outage recovery, expiry, method-version filtering and persistent deployment state. The lightweight encoder provides lexical vectors; semantic ranking and enterprise isolation require separate configuration and evaluation.

## Remaining deployment inputs and extensions

Operators provide provider/channel credentials, organization identities, real
tool commands, service scopes and domain evaluators. The included HTTP world provides controlled services for local operation and testing.

Multi-host scheduling, distributed failover, production canary cohorts,
long-duration quality studies and autonomous Harness/improver modification
remain separately gated in the [roadmap](ROADMAP.md). Single-Skill On-call
receipts retain their old evidence format; complete-bundle manifests are used
by the governed personal path and shared core without silently changing old
On-call artifact identities.
