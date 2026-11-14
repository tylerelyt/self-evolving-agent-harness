# Maintained OpenViking service

Install OpenViking 0.4.12 and the SDK into an isolated server environment:

```bash
.deps/hermes-agent/.venv/bin/python deploy/setup_openviking.py
var/openviking/venv/bin/python -B deploy/openviking.py \
  --state var/openviking/server --port 1933 --model-env-file .env \
  --max-model-requests 60
```

The private environment file accepts `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`
(default `deepseek-flash`) and `DEEPSEEK_BASE_URL` (default the official HTTPS
endpoint). Process variables take precedence. Credentials are not printed or
written into server metadata. The encoder/indexer loopback gateway requires a
new private bearer on each launch; only its 0600 configuration contains it.

The server owns an exclusive state lock. `server.json` changes from `starting`
to `ready` only after `/health` responds. SIGTERM shuts down the foreground
server; restart with the same state retains the real OpenViking filesystem and
vector index. Model request and usage accounting also survives restart; increase
the explicit total budget when the operator permits more indexing. A budget
refusal is a recorded model failure. OpenViking may still complete vector indexing
with a fallback summary; this does not certify generated-summary quality. A true
index failure leaves publication pending. The adapter requires a successful native
batch/index receipt, exact readback and native `viking_search`/`viking_read`
visibility of the precise immutable revision. OpenViking 0.4.12 can report zero
queue errors before an asynchronous vector failure arrives; the extra visibility
check prevents an unindexed raw file from being treated as delivered. Retries
write matching bytes as `unchanged` and refresh the index again. Publication
search targets that revision URI, so unrelated historical hits cannot crowd it.

## Retrieval quality boundary

The supplied lightweight encoder implements normalized character n-gram feature
hashing locally. Its query/document-dependent vectors support lexical retrieval integration
without a separate paid embedding account or local model download. It is
**not a semantic embedding model**, and this deployment does not certify
semantic recall/ranking quality. DeepSeek makes real bounded calls for
OpenViking's indexing summaries. A production encoder requires its own matched
retrieval evaluation and explicit provider configuration.

`OpenVikingProvider` publishes immutable revision documents using native
OpenViking content APIs. Reads use the pinned Hermes native OpenViking
`viking_search` and `viking_read` tools; the adapter does not implement an
alternate search engine. Exact scope and content hashes are checked again,
then the local KnowledgeStore filters current heads, expiry and revocation.
The local store is an authority/outbox, not the retrieval backend.

The default server is loopback trusted mode. URI scoping and adapter checks do
not create a remote multi-tenant authorization service. Deploy private network
boundaries and server authentication for shared organizational use.

## Explicit real-server check

With the real server already running, the following checks actually index two
revisions and retrieve them through native Hermes tools (indexing can consume
model requests):

```bash
RSI_NATIVE_OPENVIKING_ENDPOINT=http://127.0.0.1:1933 \
  .deps/hermes-agent/.venv/bin/python -B -m unittest discover \
  -s tests/core -p test_openviking_provider.py -v
```

Without the explicit endpoint the native check is skipped; pure contract checks
are labeled separately. For the lesson-14 exercise environment, use
`scripts/setup_openviking.sh`.

## Default dedicated profile

`deploy/manage.py launch --profile deploy/profiles/dedicated-oncall.json` uses
`deploy/oncall.py` to supervise this server automatically after installation.
Do not start a second server against the same workspace. The supervisor settings
control `memory.managed`, port, environment-file path and indexing budget; the
On-call process starts only after owned-server readiness is verified. An
operator-configured external endpoint is reused without taking its ownership.
