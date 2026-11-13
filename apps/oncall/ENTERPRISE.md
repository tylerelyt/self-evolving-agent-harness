# Dedicated On-call recovery integration

The maintained Workbench supports the shared [authenticated action gateway](../dedicated/README.md). With `ONCALL_ACTION_CONFIG` unset, the reference local approval workflow is unchanged. With it set, recovery must pass through the configured gateway; failures never fall back to clearing the local fault.

This integration is in `Workbench.approve_recovery`, after the existing source Session/Item, report, paging, environment and explicit human confirmation checks. It does not give the Agent the engineer's credential or let the model choose a recovery executable.

## Configure the trusted binding

Create a private file, for example `/srv/oncall/private/action-binding.json`:

```json
{
  "tenant": "acme",
  "executor": "oncall-harness",
  "gateway_url": "http://127.0.0.1:8766",
  "approver_token_env": "RSI_ENGINEER_TOKEN",
  "executor_token_env": "RSI_HARNESS_TOKEN"
}
```

Set `ONCALL_ACTION_CONFIG` to that path in the trusted Workbench server's environment. The referenced credential environment variables are private deployment configuration. They are not model parameters, persisted model context, or values accepted from browser request JSON. Keep the gateway's signing key outside both Workbench and Hermes workers. Existing Workbench confirmation identifies the configured gateway engineer principal; it does not turn the local UI into an enterprise SSO login.

The explicit Approve action creates a signed approval bound to the actual incident, HTTP environment ID, instance/service, fault generation, verified report/page digest and assigned Skill digest. It persists `recovery/<incident>/enterprise-intent.json` before dispatch. The shared gateway records its durable action intent before executing a tool. Workbench records the returned operation-bound effect in its existing incident ledger; its normal two independent healthy-window checks still determine recovery.

On a repeated confirmation or interrupted local receipt write, Workbench reconciles the gateway operation first. A missing gateway operation can be dispatched under the confirmed approval; an expired approval can be renewed only in this explicitly confirmed path after the gateway proves no dispatch intent exists. An `unknown` result is never blindly repeated. If the enterprise binding disappears while its intent remains, execution fails closed.

## Tool adapter for the actual local HTTP environment

`recovery_tool.py` is a gateway executable that uses the existing real HTTP environment and `clear_lab_fault` implementation. It restricts service and state selection to an operator-owned file. It is suitable for exercising the enterprise boundary against the shipped environment; an organization's own recovery API needs its own scoped adapter.

Private tool configuration:

```json
{
  "receipts": "tool-receipts",
  "environments": [
    {
      "tenant": "acme",
      "environment_id": "actual-environment-id-from-the-running-service",
      "instance": "instance-a",
      "state": "/absolute/workbench-state/environments/instance-a",
      "services": ["payments", "orders", "inventory", "notifications"]
    }
  ]
}
```

Add each authorized environment separately. Configure gateway principal grants as exact `(tenant, environment_id, clear_lab_fault)` triples. The tool enforces the additional instance/service restriction, checks real HTTP scope, and compares the approved fault inode/mtime generation before changing anything.

In the gateway tool map, set `clear_lab_fault` to fixed argv arrays:

```json
{
  "apply": ["/absolute/python", "/absolute/repo/apps/oncall/recovery_tool.py", "apply", "--config", "/private/tool.json"],
  "observe": ["/absolute/python", "/absolute/repo/apps/oncall/recovery_tool.py", "observe", "--config", "/private/tool.json"]
}
```

Tool receipts persist separately from the gateway journal. A repeated operation returns the stored receipt. If the tool dies after the actual side effect but before recording an attributable receipt, observation remains `unknown`; absence of a fault file alone does not invent proof that this operation caused recovery.

## Acceptance

```bash
.deps/hermes-agent/.venv/bin/python -B -m unittest discover \
  -s tests/core -p 'test_enterprise_recovery.py' -v
```

The integration check runs real HTTP services, injects an actual fault, routes the explicit Workbench approval through a real authenticated HTTP gateway and fixed tool subprocess, verifies the fault is cleared once, and rejects another environment or an unconfigured service. The native Session provenance checks have their own existing tests; this fixture stubs that verified input boundary instead of synthesizing native Session history.
