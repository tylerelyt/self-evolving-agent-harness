# Implementation and acceptance

The maintained single-host system has separate execution, learning, evaluation,
release and tool-authorization boundaries. Tutorials teach it through compatible
entrypoints. The complete target architecture also includes later distributed
and production rollout work; those extensions are not implied by local checks.

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
| Personal learning, assignments, adoption and rollback | `personal.py`, `deploy/personal.py` |
| Scoped approval and action reconciliation | `authority.py`, `apps/dedicated/`, `apps/oncall/enterprise.py` |

The core imports no applications, workshops or examples. The maintained
application runs without teaching folders, including from an installed wheel.
Old `workshop/capstone` commands execute the corresponding canonical source;
learner surfaces and independently runnable lesson examples remain their own
teaching code. Static assets have compatibility links at the old paths.

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
Model instructions now state the actual native schema constraints and require
fresh per-task reads and serial tool receipts; independent graders are unchanged.

These are single-host functional and limited real-model observations. A Skill
read proves assigned-method adoption; it does not prove every later business
result. Token usage is reported when returned by the provider; unknown billing
is not represented as free execution. Longitudinal gains need repeated held-out
runs and independent business observations.

## Recorded acceptance — 2026-11-14

- 82 native core checks passed, including actual service restart and scoped tool execution.
- 59 native On-call domain regressions passed without skips; all 13 dependency-free suites and the Node workbench checks passed.
- The actual continuous service completed two independently injected incident batches, with native Hermes/SkillClaw/HTTP, scoped paging and approved recovery. Its durable learning job completed generation/evaluation/publication checkpoints; the next two native Sessions proved the published Skill's exact content. This positive collective-learning run used the scripted model.
- Personal DeepSeek/Hermes acceptance used 11 real model requests across learning, a published task and a rollback task. Four fixed external tool cases passed after the actual script repair; native Skill and terminal receipts proved use.
- DeepSeek On-call acceptance completed four real-model investigations over two batches. Independent audit verified continued patrol, recovery, both peer recalls and diagnostic Memory revision. Both generated On-call revisions were rejected, as was the auxiliary recovery Skill; the formal baseline stayed active. This run proves guarded operation with rejected candidates, **not** real-model On-call improvement or learned-version adoption. It did not exercise a real-model On-call post-rollback patrol.
- A wheel-only runtime outside the checkout started the workbench and recurring inspections with teaching imports blocked. Dedicated recovery also passed a real loopback HTTP gateway/tool-subprocess integration.

Failed real-model runs remain available in the operator's private evidence store. None is overwritten or counted as a passing quality comparison. Organization-specific SSO, tool deployments and long-duration model quality are not certified by these local observations.

## Remaining deployment inputs and extensions

Operators provide provider/channel credentials, organization identities, real
tool commands, service scopes and domain evaluators. The included HTTP world is
a controlled service workload, not a connector to an unknown enterprise.

Multi-host scheduling, distributed failover, production canary cohorts,
long-duration quality studies and autonomous Harness/improver modification
remain separately gated in the [roadmap](ROADMAP.md). Single-Skill On-call
receipts retain their old evidence format; complete-bundle manifests are used
by the governed personal path and shared core without silently changing old
On-call artifact identities.
