# Deploy the RSI technical system

The system supports a daily personal Harness and a dedicated Harness for a workload or organization. On-call is the first maintained workload adapter.

The current deployment runner supports macOS and Linux with Python 3.12. It owns a foreground process, private workspace, runtime logs and lifecycle records. Run it on a laptop or a single host; an OS process manager can supervise the foreground command. Generic distributed scheduling and enterprise identity integrations remain separate architecture gates.

## Install pinned runtime dependencies

From the repository root:

```bash
bash scripts/setup_deps.sh
bash scripts/setup_skillclaw.sh
```

These scripts fetch the committed Hermes/SkillClaw revisions into `.deps`; they do not modify the user's default Harness configuration. The shared core itself has no model SDK dependency.

The installable core also exposes `rsi-harness` with the same `doctor`, `setup`, `launch` and `status` operations. Source-checkout examples below use `deploy/manage.py` so a separate core installation is not required. Profiles for an installed CLI use absolute adapter paths or installed module commands instead of the checkout-only `{repo}` placeholder.

## Personal daily Harness

```bash
.deps/hermes-agent/.venv/bin/python -B deploy/manage.py doctor \
  --profile deploy/profiles/personal.json
.deps/hermes-agent/.venv/bin/python -B deploy/manage.py setup \
  --profile deploy/profiles/personal.json
.deps/hermes-agent/.venv/bin/python -B deploy/manage.py launch \
  --profile deploy/profiles/personal.json
```

The interactive setup configures the actual Hermes model/provider and messaging channels in this profile's private learning `HERMES_HOME`, under `var/personal/learner`. Supply credentials there or through the launching process environment; never put credentials in a committed deployment profile. Setup requires provider and channel configuration; `doctor` checks executable availability, while authentication is established by the running application. An existing deployment using `var/personal/harness` must explicitly configure the new learner profile before switching.

Launch runs the real pinned Hermes foreground Gateway with its external-supervisor contract. Native personal Memory/Skill learning writes the learner workspace. The [governed personal task path](PERSONAL-LEARNING.md) snapshots complete Skills, runs an operator-owned paired evaluator, publishes an assignment and verifies the next task's native Skill read. Ordinary Gateway chat remains the learning surface; the profile does not claim every native self-update passed an independent benchmark.

`doctor` checks that the actual runtime executable is available. A running child is not proof that a messaging channel authenticated or a model task passed; the status record explicitly leaves application readiness unprobed.

## Dedicated On-call Harness

```bash
.deps/hermes-agent/.venv/bin/python -B deploy/manage.py doctor \
  --profile deploy/profiles/dedicated-oncall.json
.deps/hermes-agent/.venv/bin/python -B deploy/manage.py launch \
  --profile deploy/profiles/dedicated-oncall.json
```

This starts the continuous On-call service, including independent local service environments, patrols and its learning worker. The service runs until stopped and accepts externally triggered fault alerts. The profile reopens `var/dedicated-oncall/runs/current` with `--resume`, preserving approved methods and execution/learning evidence; each supervisor launch still retains a distinct deployment record and log.

The shipped workload profile uses a scripted model to verify deployment and integration. For real-model use, create a local profile with `--live` appended to its command and configure the model as described in the [application reference](../apps/oncall/README.md). Model names/endpoints are bound to a workspace; use a fresh state directory when switching providers. Real-model task quality is a separate acceptance result.

The On-call adapter uses the shared RSI candidate coordinator and ledger for the continuous candidate → evaluation → publication → adoption lifecycle. Workload recovery and operational assignment remain workload state; they are reconciled with the lifecycle ledger after interruption.

## An enterprise-specific Harness

A dedicated deployment can run an organization's Harness through a configured process command. The adapter receives:

| Binding | Meaning |
| --- | --- |
| `RSI_PROFILE` | Deployment identity |
| `RSI_STATE_DIR` | Private persistent state root |
| `RSI_SETTINGS_PATH` | Configuration/adapter settings document |
| `HERMES_HOME` | Private home for a Hermes-based adapter |

The dedicated adapter implements its domain context, tools, identity/authorization, approval integration, evidence collection and evaluation tasks. It connects method changes to `ImprovementCoordinator` and `LearningLedger`; it does not copy the RSI core. Use the [shared contracts](../docs/architecture/CONTRACTS.md) and [architecture](../docs/architecture/TARGET-ARCHITECTURE.md) as the integration boundary.

The supervisor passes profile `settings` to the adapter, which implements authorization and evaluation. The shipped local profile uses local approval semantics. The [dedicated tool gateway](../apps/dedicated/README.md) implements Bearer-authenticated principals, exact scope grants, signed short-lived approval and a persistent action journal; [bind it to On-call](../apps/oncall/ENTERPRISE.md) to enforce that boundary on actual recovery. An unavailable configured gateway never falls back to local execution. Organization SSO and real service tool commands are supplied by the operator.

Different state directories isolate configuration/history, not operating-system authority. Organizations needing credential and tenant isolation deploy under separate service identities or containers and enforce scoped tools in their Harness adapter. Inherited environment values remain part of the chosen process's credential context.

## Ownership, shutdown, restart and evidence

```bash
.deps/hermes-agent/.venv/bin/python -B deploy/manage.py status \
  --profile deploy/profiles/personal.json
```

Each workspace has one effective supervisor owner. A second launch or setup against that workspace is rejected while it is active. Runtime commands are argument arrays and never execute through a shell.

Ctrl-C or SIGTERM requests graceful shutdown of the owned runtime process group. The profile defines the grace period. Forced shutdown is recorded; it is not proof that an arbitrary custom Harness reconciled every side effect. Adapters must stay in the foreground and clean up their own resources; OS-managed deployments should also own the runtime control group.

The current status is `deployment.json`; each launch also preserves `deployments/<run-id>.json` and a separate runtime log. `settings.json`, the Harness home, artifacts and learning state live in the workspace and are not deleted on shutdown. Back up state after quiescing the process, including its SQLite stores and artifact references.

The personal learner home persists across launches, and governed tasks retain immutable assignments. The dedicated On-call profile resumes the same state, checks its runtime binding, reconciles interrupted publication before worker startup, and resumes pending incidents with fresh observations. This is single-host resumption; distributed ownership and cross-host recovery remain in the [roadmap](../docs/architecture/ROADMAP.md).

## Managed persistent OpenViking memory

The default dedicated profile declares `settings.memory.backend=openviking`
and `managed=true`. Prepare its isolated server environment first:

```bash
.deps/hermes-agent/.venv/bin/python deploy/setup_openviking.py
```

`deploy/oncall.py` reads the supervisor's `RSI_SETTINGS_PATH`, starts the real
OpenViking server under `<state>/openviking`, verifies that this exact child PID
has published `server.json` as ready and its endpoint passes health checks, then
binds the continuous application to it. The local revision authority/outbox is
under `<state>/knowledge`; OpenViking remains the actual retrieval backend.
Both stores and their stable namespace survive restart.

The default patrol model is still scripted, but memory indexing uses real
DeepSeek summaries and requires the private repo `.env` or
`settings.memory.model_env_file`. These indexing calls consume the configured provider's model budget.
`max_model_requests` caps indexed-summary requests across server restarts;
usage is recorded privately in the server workspace. The default embedding
encoder is local lexical feature hashing, not a certified semantic model.
See [OpenViking operation and quality boundaries](OPENVIKING.md).

Memory settings accept a trusted absolute `server_python`, `server_state_dir`,
`state_dir`, `port`, `model_env_file`, `max_model_requests` and
`startup_timeout`. Relative state paths resolve under the deployment workspace.
Port 0 selects and persists an available loopback port on first launch; resume
reuses that endpoint. A trusted `server_command` argv can integrate an
operator-owned launcher using `{state}`, `{port}` and `{repo}` placeholders;
it is not executed through a shell.

To use an existing server, set `managed=false` and an explicit `endpoint`; the
launcher does not own or stop that external process. Existing account/user and
API-key-environment bindings remain supported. Missing dependencies or initial
server readiness fail launch explicitly, rather than disabling memory or
substituting a SQLite search implementation. After startup, a memory outage is
reported by the application as degraded; it does not terminate patrol solely
because indexing/search is temporarily unavailable.

Ctrl-C/SIGTERM stops the On-call resources and owned OpenViking process, while
preserving both stores. Changes to environment identities and runtime bindings
remain subject to the service's explicit resume/migration checks; old evidence
must not be deleted to bypass a binding mismatch.
