# Personal learning and governed execution

The personal deployment runs the real pinned Hermes Gateway. Its private
`HERMES_HOME` is `<state>/learner`; setup writes provider/channel configuration
there. Ordinary chat and native learning can change this learner workspace.
Those changes do not change a formally assigned Skill.

Governed tasks use the same launcher, with a fresh Hermes home containing a
complete published Skill bundle. This path requires independent evaluation
before changing the assignment, then verifies actual `skill_view` output from
the next task's native SessionDB. A load receipt proves use of the assigned
method; it does not establish that every downstream business action succeeded.

From the checkout, use the Hermes Python for all commands:

```bash
.deps/hermes-agent/.venv/bin/python deploy/manage.py setup --profile deploy/profiles/personal.json
.deps/hermes-agent/.venv/bin/python deploy/manage.py launch --profile deploy/profiles/personal.json
```

Register an operator-reviewed starting Skill and propose a native learned copy:

```bash
.deps/hermes-agent/.venv/bin/python deploy/personal.py govern --state var/personal \
  seed --name service-check --source /absolute/path/to/reviewed/service-check
.deps/hermes-agent/.venv/bin/python deploy/personal.py govern --state var/personal \
  propose --name service-check --source var/personal/learner/skills/service-check \
  --source-session actual-session-id:actual-report-item
```

`seed` is an explicit operator bootstrap, not a learned release. Full bundles
include scripts, references, assets and executable bits. Candidate identity is
the SHA-256 of the canonical full manifest, rather than only `SKILL.md`.

Supply an operator-owned evaluator argv JSON outside the candidate tree:

```json
["/absolute/python", "/operator/eval_service_check.py", "{baseline}", "{candidate}", "{receipt}"]
```

The command runs baseline and candidate against independent controlled tasks,
observes actual tool behavior, and writes a JSON receipt to `{receipt}`:

```json
{
  "protocol": "paired-tool-behavior-v1",
  "baseline_sha256": "SHA256_OF_BASELINE_MANIFEST",
  "candidate_sha256": "SHA256_OF_CANDIDATE_MANIFEST",
  "cases": [
    {"baseline": {"valid_trace": true, "passed": false},
     "candidate": {"valid_trace": true, "passed": true}}
  ]
}
```

The evaluator gets content-tree paths; their parent directories contain the
canonical manifests. Model, task data, budget and scorer must remain matched.
Store tool traces with the evaluator report. The evaluator is trusted operator
code: this adapter validates its protocol and gate, not arbitrary domain truth.
The core requires complete valid traces, no failing candidate cases and at
least one repaired baseline failure. A rejected candidate leaves formal use
unchanged.

```bash
.deps/hermes-agent/.venv/bin/python deploy/personal.py govern --state var/personal \
  evaluate CANDIDATE_ID --evaluator /operator/evaluator-argv.json
.deps/hermes-agent/.venv/bin/python deploy/personal.py govern --state var/personal \
  publish CANDIDATE_ID
.deps/hermes-agent/.venv/bin/python deploy/personal.py govern --state var/personal \
  task --name service-check --prompt 'Inspect the authorized service.'
```

Each task retains `assignment.json`, native `home/state.db`, `execution.json`,
private runtime logs, native `usage.json` and a verified `adoption.json`. The
runner uses pinned Hermes `--oneshot` with explicit `skills,terminal` toolsets;
`--toolsets` can select another operator-authorized set. Native oneshot bypasses
interactive tool approvals, so tasks must be explicitly authorized for their
execution environment. The separate On-call recovery approval gate is unchanged.
If Hermes omits the Skill
read, changes the assigned bundle, or fails, the task cannot establish adoption.
Dynamic Skill preprocessing that changes the body intentionally fails this
strict byte-matching acceptance; register a static governed Skill for this path.

Publication records intent before atomically changing the assignment. After
interruption, `govern ... reconcile CANDIDATE_ID` inspects the actual assignment
and immutable bundle without repeating publication. Do not edit assignment or
ledger files by hand.

`govern ... rollback CANDIDATE_ID` restores the evaluated parent assignment for
future tasks, with a persisted rollback intent and verified bundle receipt.
`reconcile` also completes interrupted rollback records. Already running tasks
retain their pinned copy; this does not undo their completed business actions.

These directories isolate native updates and preserve provenance for a trusted
single user. They are not a security sandbox: code with the same OS identity
can write elsewhere. Enterprise deployments must separately restrict runtime,
evaluator, artifact and action authority using service identities/containers.
Personal Gateway chat is the learning surface; only the `govern task` path is
claimed to consume evaluated assignments. Real-model task quality remains a
separate acceptance run.

## Verification scope

A bounded DeepSeek Flash run through pinned native Hermes verified the personal
learning path. The learner repaired a checker script, and an operator-owned
paired evaluator compared the complete bundles on four frozen HTTP-status
cases: healthy, intermittent, persistent failure and a late failure. The
candidate passed all four and repaired two baseline failures. Subsequent native
tasks read and executed the published bundle, then the parent bundle after
rollback; SessionDB tool items and immutable bundle hashes verified adoption.

These results establish that this integration can change, evaluate, publish and
restore a method on the tested cases. They do not establish general task quality
or coverage of every native personal update. Native usage reports can have
`cost_status: unknown`; a numeric zero estimate in that case does not establish
zero provider cost.
