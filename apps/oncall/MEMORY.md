# Persistent On-call memory

The maintained On-call application uses OpenViking to retrieve historical incident facts and approved runbooks. Native Hermes SessionDB remains the original execution evidence. The local `KnowledgeStore` is the revision authority and durable publication outbox; it validates retrieved citations and is not an alternative search backend.

## Deployment

The default `deploy/profiles/dedicated-oncall.json` starts a real, pinned OpenViking 0.4.12 server through the deployment supervisor. Prepare it with `deploy/setup_openviking.py`, then follow the [dedicated deployment commands](../../deploy/README.md#dedicated-on-call-harness). The supervisor checks readiness of its own child process and stops that process on shutdown. An existing server can be selected with `settings.memory.managed=false` and an explicit endpoint; the launcher does not take ownership of that process.

The default deployment stores OpenViking data under `var/dedicated-oncall/openviking` and revision state under `var/dedicated-oncall/knowledge`. Both survive restart. Indexing summaries use the private DeepSeek configuration; the indexing request cap and usage ledger are persisted. The default embedding encoder uses local lexical feature hashing. A working native server and valid citations demonstrate the retrieval integration, not semantic search quality or real-model On-call quality. See [OpenViking operation and quality boundaries](../../deploy/OPENVIKING.md).

For direct service or workbench use, supply an operator-owned template:

```json
{
  "schema_version": 1,
  "backend": "openviking",
  "endpoint": "http://127.0.0.1:1933",
  "tenant": "oncall",
  "state_dir": "/absolute/path/oncall-knowledge",
  "retrieval_timeout": 5,
  "publication_timeout": 100,
  "fact_ttl_seconds": 2592000
}
```

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall serve \
  --out /absolute/path/oncall-run --memory-config /absolute/path/memory.json
```

`workbench --memory-config /absolute/path/memory.json` uses the same contract. Store credentials in the environment named by `api_key_env` (default `OPENVIKING_API_KEY`), never as an `api_key` value in the template. HTTPS endpoints and loopback HTTP are supported. The service binds the template to the two actual HTTP environment identities before starting patrol. Its absolute binding path is recorded in `service.json` as `memory_binding` and propagated to child processes through `ONCALL_MEMORY_CONFIG`.

The revision directory's `identity.json` preserves the deployment namespace and tenant/endpoint/account/user binding. Changing these values in place is rejected. Environment IDs and service scope are Harness-controlled; the model cannot choose another tenant or environment by passing tool arguments.

## From a closed incident to published knowledge

1. External monitoring alerts Hermes. The current turn performs On-call investigation under its pinned Skill, pages a human with a conclusion and recovery proposal, and returns to patrol. Human approval governs recovery.
2. After recovery is verified by fresh observations and the incident is closed, the independent memory worker finds the incident. It checks the initial report, investigation fork, paging report and recovery evidence against their actual stored records.
3. A leased review job runs the fact-review agent. Its proposal must satisfy the existing evidence contract. A missing or inconsistent source causes an explicit source/review error; no plausible substitute fact is published.
4. An accepted proposal becomes an immutable incident revision and an outbox entry. The worker also records the runbook read from the actual protected Skill when that Skill is still the approved method.
5. The outbox publishes immutable revisions to OpenViking and retains the returned URI, revision ID and content digest. Failed delivery remains pending for retry. Only a delivered current revision can be returned to a model.

This worker runs independently of SkillClaw's candidate-learning loop. Remembering a recovered incident does not approve a new Skill, and a queued publication does not mean the fact is searchable.

## Revisions, sources and validity

Each revision carries an exact `{tenant, environment, service}` scope, a logical key, content, original sources, its parent revision, state and optional `valid_until`. Incident keys use `incident:<incident-id>`; runbooks use `runbook:<skill-name>`. Updating a key uses compare-and-swap against its current revision. Prior revisions remain immutable, including their evidence and delivery history. Revocation creates a new inactive head rather than deleting history.

Sources identify the real `session_id`, `item_id`, SHA-256 of the original stored message content and the SessionDB evidence path. The review worker verifies those source bytes before committing knowledge. Provider citations also carry a URI, revision ID and content SHA-256; retrieval checks them against the delivered revision receipt. Historical assertions therefore retain a traceable path to the original native Hermes execution.

The default fact TTL is 30 days. Incident validity is measured from the verified recovery window; runbook validity is measured from revision creation. Superseded, revoked, expired, undelivered and unknown remote revisions are excluded from recall. A replacement waiting for publication does not revive the old head. Runbook recall additionally checks that its content hash matches the runtime's currently approved Skill. A successful fact review cannot silently rewrite an already verified incident with different content.

## Retrieval during real execution

With memory configured, native Hermes receives a zero-argument `knowledge_lookup` tool and may invoke it during patrol or investigation. The Harness binds the service, instance, environment and current Session. Patrol must observe the current HTTP service before retrieving knowledge; investigation performs its own new lookup rather than treating references inherited by a fork as a fresh read. The deterministic wiring model also invokes the real tool when configured, preserving the same SessionDB receipt without claiming model reasoning quality.

The result explicitly states `backend: openviking`, `historical_only: true`, scope, facts/runbooks and citation metadata. `success` describes the retrieval operation: recalling an old TimeoutError is a successful read, while an unavailable provider is a failed read. Raw tool results remain in the actual Hermes Session/Item chain, so SkillClaw feedback can distinguish historic incident text from a tool transport failure.

OpenViking is consulted on every model retrieval. SQLite is used afterward to verify authority, current version, validity and the returned citation bytes. There is no local search fallback during a provider outage. The default read deadline is five seconds for the complete native search/read operation; publication has a separate 100-second timeout. Errors are explicit and sanitized. An error preserves the evidence gap and patrol continues using fresh HTTP observations. Historical memory never establishes current service health or confirms the current root cause.

Workbench SRE conversations use the same scoped tool. The Memory panel reads publication metadata for administration: backend availability, worker state, pending outbox, and delivered current facts/runbooks. It does not demonstrate a model search and does not count queued facts as published. The legacy manual SQLite review/recall controls are disabled when OpenViking memory is active; verified incident closure triggers review automatically.

## State and resumption

The memory directory contains `knowledge.sqlite3` (immutable revisions, heads, delivery receipts and outbox), `jobs.sqlite3` (leased review work and retry state), `reviews/` (review attempts), `identity.json` (stable namespace) and `status.json` (last worker/backend status). Service logs include `system/memory-worker.log`; `system/memory-binding.json` records actual runtime scope. Back up the original SessionDBs and referenced files together with this state after quiescing the runtime.

To enable memory on an existing service, explicitly resume with the additive migration flag:

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall serve \
  --out /absolute/path/existing-run --resume --enable-memory \
  --memory-config /absolute/path/memory.json
```

This preserves environment identities, approved Skill bytes and original Sessions/Items. It only permits first-time memory enablement; incompatible model, implementation, environment or memory bindings still fail rather than starting a fresh run silently. Dedicated deployment adds this flag explicitly. System-mode workbench restart resumes its active run when code/model bindings match and carries an explicit first memory configuration into that migration. Tutorial mode retains its separate fresh-run behavior.

The worker uses fenced leases and a durable outbox to recover interrupted work. Publication retry never marks undelivered knowledge as current searchable evidence. The deployment supervisor requires the configured server to become ready at launch; later search/indexing outages are visible degraded states and do not stop patrol by themselves. Direct application processes also report unavailable memory explicitly. Inspect worker status, pending deliveries and original source receipts before interpreting an empty result as absence of historical incidents.
