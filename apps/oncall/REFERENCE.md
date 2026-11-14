# On-call operation reference

[Quick entry](README.md). Run `make setup` once; commands below use the pinned Python from the repo root.

## Continuous service

The default [dedicated deployment profile](../../deploy/profiles/dedicated-oncall.json) includes a managed, persistent **real OpenViking server**. Install its pinned runtime and launch through the supervisor:

```bash
.deps/hermes-agent/.venv/bin/python deploy/setup_openviking.py
.deps/hermes-agent/.venv/bin/python -B deploy/manage.py launch \
  --profile deploy/profiles/dedicated-oncall.json
```

The profile resumes the same runtime, environments and knowledge namespace. It requires private DeepSeek configuration for OpenViking's indexing summaries, even when the patrol model is scripted. The default local lexical embedding encoder verifies retrieval plumbing; semantic retrieval quality needs separate evaluation. See the [memory contract and operation reference](MEMORY.md) and [deployment setup](../../deploy/README.md#managed-persistent-openviking-memory).

For a direct service process without deployment-managed memory:

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall serve \
  --out /tmp/oncall-system-run
```

By default, use a new output directory. This starts two local HTTP environments, recurring Hermes inspections, On-call handling and a background learner. It stays running until interrupted; it does not inject a fixed number of incidents automatically. The default is an explicitly recorded scripted model.

Add `--memory-config /absolute/path/memory.json` to bind an existing OpenViking server. A separate memory worker automatically reviews incidents after verified recovery, publishes sourced facts and current approved runbooks through a durable outbox, and retries failed publication. Patrol and investigation use the real `knowledge_lookup` model tool; its original result remains in the native Hermes Session/Item chain. Retrieval has a five-second deadline by default. An unavailable server produces an explicit tool error while patrol continues; retrieval never falls back to SQLite. Local revision metadata only validates provider-returned citations.

External monitoring produces alerts; the local environment can inject controlled faults. State, service ownership, logs, Sessions and learning records live under the output directory. The [external action boundary](ENTERPRISE.md) connects approved recovery to authenticated tools; the built-in environment remains a local HTTP workload.

Use `--patrol-only` to exercise inspection without On-call handling, or `--no-learning` to keep On-call workers active without the learner. Ctrl-C and SIGTERM clean up owned processes and preserve evidence. Use `--resume` to create or reopen the same directory with the same runtime binding. It preserves approved Skill bytes, learning jobs, patrol records, native Sessions/Items, paging and environment identities; HTTP windows are measured afresh. Changes to model, endpoint, implementation, patrol scope or environment bindings require a new workspace. A second owner is rejected.

For DeepSeek, configure a private repo `.env` (or use `ONCALL_MODEL_ENV_FILE`):

```dotenv
ONCALL_PROVIDER=deepseek
DEEPSEEK_API_KEY=YOUR_PRIVATE_KEY
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

Then add `--live`. GLM remains supported through `ONCALL_PROVIDER=glm` and the existing `GLM_API_KEY`, `GLM_MODEL`, `GLM_BASE_URL`. An explicit provider binds all transport settings together; keys are never borrowed across providers. The pinned integrations retain legacy GLM transport variable names internally.

The model default follows [DeepSeek's current API documentation](https://api-docs.deepseek.com/); select an explicitly supported model when upgrading. Real-model quality and scripted wiring are recorded separately.

## Workbench

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall workbench \
  --offline --state /tmp/oncall-workbench --new-state --port 8765
```

This opens system mode: runtime management and the complete maintained implementation, without Coding Agent exercises. `--tutorial` selects the separate learner interface; the original workshop entry defaults to tutorial mode. The workbench is a view/control surface over the same execution, evidence and learning behavior.

Use `--memory-config /absolute/path/memory.json` to connect the workbench's service to operator-owned OpenViking. System mode preserves environment identities across reopen and resumes the same active run when code, model and runtime bindings match. Approved Skill bytes and native Session/Item evidence continue from that run; changed code or model creates a separate run. An explicit first memory configuration adds memory to the preserved run. Binding rejection remains an error. Tutorial mode keeps its independent fresh-run behavior. The Memory panel reports real backend availability, pending publication and published current facts; it is a management view, while model recall goes through OpenViking.

## Finite acceptance rehearsal

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall demo \
  --out /tmp/oncall-acceptance-run
```

This controlled verification run injects two rounds of local faults and exercises recovery, Memory revision, collective Skill learning, comparison and subsequent adoption. It is an acceptance scenario, not the lifetime of the service.

The shared candidate ledger is written to `system/learning.sqlite3` in both continuous service and the rehearsal. Reports and continuous release state link candidate identities and evaluation digests. The ledger records proposal, evaluation, publication intent, verified delivery, per-instance adoption and verified restoration. It does not execute or authorize side effects.

The continuous service retains workload assignment and operational recovery state in `learning/state.json`, linked to the shared candidate lifecycle. Restart reconciles interrupted delivery before admitting a new learning batch; an orphaned pre-delivery intent is closed only after checking the preserved parent and both worker entrypoints. A shared rollback record means restored method bytes; post-rollback task verification remains a separate operational check.

## Implementation boundary

[Source map](README.md#change-it) · [architecture](../../docs/architecture/README.md). Application execution does not import teaching folders.

Continuous learning uses the shared SQLite `ControlStore`: immutable batch inputs, lease/heartbeat, fenced phase checkpoints and durable bounded retries. Publication remains governed by the candidate ledger and workload reconciliation, never by a queue success flag alone.

The installable wheel includes the maintained applications, static UI, Skills, integrations and core. Set `HERMES_SRC` to the clean pinned Hermes checkout when executing outside the repository. `rsi-oncall` is the installed application entry; existing tutorial commands are compatibility wrappers.

For a bounded real-model run with request and usage evidence:

```bash
.deps/hermes-agent/.venv/bin/python -m apps.oncall.acceptance \
  --out /tmp/oncall-real-acceptance --max-requests 320 --max-seconds 900
```

This uses actual HTTP services, Hermes and SkillClaw; it preserves failed runs and stops rather than claiming success after budget exhaustion. Usage records contain provider token counts; no currency cost is fabricated when the provider does not return billing data.

Multi-host scheduling, organization-specific SSO/tool integration, production canaries and Harness/improver self-modification remain in the [roadmap](../../docs/architecture/ROADMAP.md).
