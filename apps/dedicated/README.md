# Dedicated Harness action boundary

This is a runnable single-host adapter for scoped tool execution. It authenticates separate engineer and Harness principals, issues expiring approvals, persists action intents before dispatch, and reconciles uncertain tool outcomes. It reuses the shared `ActionJournal`; it does not implement an organization's SSO, business API, or distributed executor.

The On-call workload can use `GatewayClient` in its trusted recovery adapter. The Agent proposes a recovery; an authenticated engineer approves the exact action. Only the executor credential calls dispatch. The model never receives the engineer credential, signing key, or tool-command configuration.

## Configure the gateway

Create a private configuration outside the repository. An example shape follows; executable paths must be replaced with real absolute paths to operator-maintained adapters:

```json
{
  "schema_version": 1,
  "signing_key_env": "RSI_APPROVAL_SIGNING_KEY",
  "journal": "actions.sqlite3",
  "tool_timeout": 30,
  "principals": [
    {
      "name": "oncall-engineer",
      "token_env": "RSI_ENGINEER_TOKEN",
      "roles": ["approve"],
      "grants": [["acme", "staging", "restart"]]
    },
    {
      "name": "oncall-harness",
      "token_env": "RSI_HARNESS_TOKEN",
      "roles": ["execute"],
      "grants": [["acme", "staging", "restart"]]
    }
  ],
  "tools": {
    "restart": {
      "apply": ["/absolute/path/to/python", "/absolute/path/to/restart_adapter.py", "apply"],
      "observe": ["/absolute/path/to/python", "/absolute/path/to/restart_adapter.py", "observe"]
    }
  }
}
```

Supply independent private environment credentials, each at least 32 characters; never commit them. The signing key must remain outside the Harness process. Run from the repository root:

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.dedicated \
  --config /absolute/private/path/gateway.json --port 8766
```

The server binds loopback only. For remote access, use an authenticated TLS transport/reverse proxy appropriate to the organization; do not expose plaintext bearer credentials. File permissions and service identities must prevent the execution worker from reading gateway configuration, signing credentials, or its action database. Separate process state is not an OS security boundary.

The process supervisor can run this command through its ordinary dedicated profile command array. Start the gateway and Harness as distinct service identities in deployments requiring credential isolation.

## Approval and execution

```python
from apps.dedicated.client import GatewayClient

spec = {
    "tenant": "acme", "environment": "staging", "incident": "incident-42",
    "action": "restart", "parameters": {"service": "api"},
    "evidence_sha256": report_digest,
    "capability_sha256": assigned_skill_digest,
}
# These clients belong to different authenticated processes in deployment.
engineer = GatewayClient("http://127.0.0.1:8766", engineer_token)
worker = GatewayClient("http://127.0.0.1:8766", harness_token)
approval = engineer.approve("oncall-harness", spec, ttl=120)
receipt = worker.dispatch("incident-42-restart-1", spec, approval)
```

Scopes are exact `(tenant, environment, action)` triples. Approval is bound to the journal's persistent identity, executor, incident, parameters, evidence digest, assigned capability digest, and validity period. Tokens cannot cross journals even with the same signing key. The trusted workload adapter must verify its report and actually loaded capability before submitting those digests; the gateway cannot infer the Agent's loaded context.

A configured executable receives one JSON document on stdin:

```json
{"operation":"incident-42-restart-1","spec":{"tenant":"acme","environment":"staging","incident":"incident-42","action":"restart","parameters":{"service":"api"},"evidence_sha256":"...","capability_sha256":"..."}}
```

The command is a fixed argv array; request data never selects a shell or executable. A minimal inherited environment excludes gateway credentials. Organizational tools should use their own scoped service identity or credential mechanism.

For `apply`, return a nonempty JSON receipt containing the same `operation` after checking the actual external result. For `observe`, return independently observed `operation`, `state` (`completed`, `not_applied`, or `unknown`) and supporting evidence. An external API's idempotency key should also be this operation ID where supported.

A timeout, process crash, malformed receipt, or failed command leaves the committed intent `unknown`. Repeating dispatch returns the recorded state without repeating the effect. An engineer calls `engineer.reconcile(operation)` to query the tool's own state; it cannot race an in-flight effect on this host. A `not_applied` observation does not automatically retry: an explicit new approved operation is required. This prevents unsafe retry but is not a claim of distributed exactly-once execution.

## Verification

```bash
.deps/hermes-agent/.venv/bin/python -B -m unittest discover \
  -s tests/core -p test_authority.py -v
```

Checks exercise actual loopback HTTP authentication and a child-process tool, scope/parameters/evidence/version binding, forged and expired approvals, cross-journal replay, concurrent dispatch, a child killed immediately after its external effect, and durable reconciliation without repeating the effect. These local checks do not certify integration with an unknown organization's tools or identity provider.
