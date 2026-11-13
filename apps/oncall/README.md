# Maintained On-call application

The application has three modes: continuous service, a finite acceptance rehearsal, and an interactive workbench. Tutorial compatibility entries call the same implementation.

Run from the repository root with Python 3.12 and the pinned integrations:

```bash
bash scripts/setup_deps.sh
bash scripts/setup_skillclaw.sh
```

## Continuous service

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall serve \
  --out /tmp/oncall-system-run
```

By default, use a new output directory. This starts two local HTTP environments, recurring Hermes inspections, On-call handling and a background learner. It stays running until interrupted; it does not inject a fixed number of incidents automatically. The default is an explicitly recorded scripted model.

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

## Finite acceptance rehearsal

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall demo \
  --out /tmp/oncall-acceptance-run
```

This controlled verification run injects two rounds of local faults and exercises recovery, Memory revision, collective Skill learning, comparison and subsequent adoption. It is an acceptance scenario, not the lifetime of the service.

The shared candidate ledger is written to `system/learning.sqlite3` in both continuous service and the rehearsal. Reports and continuous release state link candidate identities and evaluation digests. The ledger records proposal, evaluation, publication intent, verified delivery, per-instance adoption and verified restoration. It does not execute or authorize side effects.

The continuous service retains workload assignment and operational recovery state in `learning/state.json`, linked to the shared candidate lifecycle. Restart reconciles interrupted delivery before admitting a new learning batch; an orphaned pre-delivery intent is closed only after checking the preserved parent and both worker entrypoints. A shared rollback record means restored method bytes; post-rollback task verification remains a separate operational check.

## Implementation boundary

- `service.py` owns the continuous service and learning loop.
- `workflow.py` owns the finite end-to-end verification orchestration.
- `patrol/` owns HTTP environments, inspection, incident evidence, evaluation and SkillClaw adapters.
- `workbench*.py` and `web/` own the maintained control surface.
- `integrations/` owns runtime compatibility, dataset/scorer and optional optimizer integrations.
- `paths.py` locates maintained modules; application execution does not import teaching folders.
- `src/self_evolving_harness` contains the extracted, tutorial-independent core.
- Existing workshop entry files are compatibility wrappers, not a second maintained service or reference workflow.

Continuous learning uses the shared SQLite `ControlStore`: immutable batch inputs, lease/heartbeat, fenced phase checkpoints and durable bounded retries. Publication remains governed by the candidate ledger and workload reconciliation, never by a queue success flag alone.

The installable wheel includes the maintained applications, static UI, Skills, integrations and core. Set `HERMES_SRC` to the clean pinned Hermes checkout when executing outside the repository. `rsi-oncall` is the installed application entry; existing tutorial commands are compatibility wrappers.

For a bounded real-model run with request and usage evidence:

```bash
.deps/hermes-agent/.venv/bin/python -m apps.oncall.acceptance \
  --out /tmp/oncall-real-acceptance --max-requests 320 --max-seconds 900
```

This uses actual HTTP services, Hermes and SkillClaw; it preserves failed runs and stops rather than claiming success after budget exhaustion. Usage records contain provider token counts; no currency cost is fabricated when the provider does not return billing data.

Multi-host scheduling, organization-specific SSO/tool integration, production canaries and Harness/improver self-modification remain in the [roadmap](../../docs/architecture/ROADMAP.md).
