# Engineering roadmap

The roadmap tracks system readiness independently of tutorial completion. Milestones have behavioral gates rather than calendar promises. A checked tutorial or successful scripted walkthrough does not close a real-model or operational gate.

## Baseline and current boundary

The recorded reference baseline is commit `8fcd907419fa002ef385bd76683413151edd92b6`. It has a controlled, scripted-model full workflow, real local HTTP/Hermes/SkillClaw integration, interruption/retry evidence, automated regressions and a screenshot walkthrough.

The maintained package includes application runtime, workbench, integrations and shared core; old teaching commands remain wrappers. Personal governed learning has real-model acceptance. On-call real-model and long-duration quality are separate gates; the full generic contracts and distributed architecture below remain incremental work.

## M0 — Preserve and finish the current baseline

Carry forward the runnable reference, evidence readers, dependency pins and isolated learner workspace. Preserve old evidence when starting a new run.

The 2026-11-14 run completed real-model incidents, Memory reuse and rejected-candidate handling; the [acceptance record](IMPLEMENTATION.md#recorded-acceptance--2026-11-14) separates that result from actual learned-version adoption.

Acceptance and follow-through:

- Complete a real-model inspection → alert → investigation → paging → authorized recovery → learning → evaluation → publication → subsequent adoption run.
- Record model configuration, actual tools, costs, independent outcomes and failures; a rejected candidate with correct continued operation is a valid system outcome.
- Exercise repeated incidents and controlled interruptions without losing ownership, evidence or pending action state.

The deterministic workflow remains useful for regression. It is labeled separately from real-model quality results.

## M1 — Extract one maintained core

Implemented: the complete application runtime, workload adapters, workbench, Skills and integrations live in `apps/oncall`; tutorial files delegate to them. The wheel runs without teaching folders. Single-Skill compatibility and complete Skill bundle manifests, release comparison and candidate lifecycle coordination are shared core code. Personal, continuous On-call and controlled verification use the shared lifecycle. The larger proposed package decomposition remains a refactoring direction rather than a readiness condition.

Introduce the package and application boundaries from [target architecture](TARGET-ARCHITECTURE.md), starting with stable contracts and adapters. Move one coherent responsibility at a time with its checks and evidence readers. Keep current exercise commands as compatibility wrappers.

Acceptance:

- The maintained On-call application runs the same end-to-end behavior through the extracted core.
- Tutorials and application call that implementation; there is no second maintained reference Agent loop.
- The core imports neither tutorial/learner code nor course-specific numbering or screenshot orchestration.
- Old baseline evidence remains readable, or a tested migration explicitly maps it.

Start with identity/version references and artifact handling, then execution/evidence, learning/evaluation and release/adoption. Avoid a repository-wide rename before these seams work.

## M2 — Make execution and learning durable

Implemented single-host slice: the shared control store has immutable jobs/events, leases, epochs, checkpoints, bounded retries and SQLite backup. Continuous learning actually uses it. On-call `--resume` preserves Sessions/Items, incidents, paging, approved methods and learner state, reconciles publication before worker startup, preserves environment identity and deduplicates monitoring intent after restart. Scoped side effects have a separate durable action journal; unknown results require reconciliation. Multi-host ownership and general cross-host task recovery remain open.

Persist commands, task attempts, checkpoints, action intents, Episodes, candidate jobs and their retry state. Start with a single-host backend; keep owner/epoch semantics compatible with later distributed execution.

Acceptance:

- Crash and restart execution and learning independently, including after action dispatch but before receipt persistence.
- Duplicate alerts/jobs do not duplicate incident work, paging, recovery or formal publication.
- Unknown action outcomes remain unresolved until reconciled; stale owners cannot commit new work.
- Backups and restore reconstruct a consistent task/evidence/artifact view.

## M3 — Establish trustworthy quality evaluation

Build versioned event-grouped datasets, independent service worlds and protected truth. Separate development, selection and final acceptance. Preserve real-model evidence across multiple independent incidents and learning batches.

Acceptance:

- New/old versions run under matched model, Memory, environment and budget conditions.
- Tests measure actual tool behavior and business results, including healthy services, intermittent failures, incomplete evidence, stale facts and unauthorized recovery requests.
- Missing data, repeated measurements, dataset leakage and scorer changes are handled explicitly.
- Claims about retained improvement have repeated, held-out evidence and uncertainty; no universal improvement percentage is promised in advance.

## M4 — Operate governed releases

Complete Skill bundle manifests, personal assignments/adoption/rollback and authenticated scoped tool approvals now exist. On-call can enforce the dedicated gateway for recovery; unavailable configured authority cannot fall back to local execution. Extend these single-host mechanisms with production canaries, predeclared stop conditions and organization-owned identity/tool integration before exposing real business services.

Acceptance:

- Interrupted publication and rollback resume without duplicating work or falsely reporting success.
- Concurrent incidents retain consistent assigned versions; fresh tasks prove adoption.
- A missing instance receipt stays visible as partial completion.
- Operational and quality monitoring can stop exposure independently; restore and incident-response drills pass.

## M5 — Evolve Harness and improver components

Extend candidate kinds to bounded runtime patches and learning/search methods. Use isolated workspaces, pinned initial capabilities, research lineage, matched budgets and independent descendant-quality evaluation.

Acceptance:

- New improvers are compared with the current one using the same starting point, experience and total budget.
- Selection tasks are separated from external transfer checks.
- Runtime compatibility, cancellation, persistence and policy checks pass alongside task quality.
- A candidate cannot alter its own final scorer, authorization floor, release gate or protected truth.

This is an extension milestone. It does not redefine Memory/Skill learning as outside the project's Harness RSI scope.

## Release and tutorial policy

Use system releases to identify shipped interfaces and behavior. Candidate and artifact versions are immutable content identities, not system release numbers. Schema versions and tutorial revisions are separate identifiers.

A tutorial pins a real system release or full commit and upstream dependency revisions. New system behavior is accepted first, then teaching material is updated. A tutorial-only explanation or exercise change can ship against an unchanged system baseline.

Until the first extracted system release exists, [tutorial compatibility](../../tutorials/compatibility.json) records the current full commit and the limits of its verification. No prospective release number is presented as already available.

Every milestone review records completed behavior, evidence, unresolved failures and the next engineering slice. Tutorial completion and system readiness remain separate progress measures.
