# ADR 0001: Shared runtime and versioned exercises

Date: 2026-11-13

Status: accepted.

## Context

On-call execution, evidence handling, learning, evaluation and publication are used by continuous deployments and programming exercises. These callers require consistent behavior while retaining different configuration, inputs and execution lifetimes.

## Decision

Shared lifecycle and persistence code lives in `src/self_evolving_harness/`. Workload implementations, the workbench and integration adapters live in `apps/`. Deployment profiles and supervision live in `deploy/`. Exercises in `examples/` and `workshop/` have isolated inputs and state; integrated entrypoints call the maintained application.

Dependencies flow from applications and exercise entrypoints toward the core and its adapter contracts. Learner implementations provide the bounded functions specified by their exercise. Runtime modules use the maintained implementation by default.

Architecture and contract documentation lives in `docs/architecture/`. Exercise compatibility records identify full system commits and upstream dependency revisions.

## Consequences

- Runtime changes are validated through their public behavior and persistence contracts.
- Exercises specify a baseline, editable surface, setup, observations and checks.
- Supported exercise baselines can remain pinned while application releases advance.
- Breaking interfaces require migration and compatibility checks for affected callers.
- Constructed faults and deterministic model fixtures remain explicit exercise or test configuration.

## Alternatives

A separate implementation for exercises would duplicate execution and release logic, increasing the risk of behavior drift. Shared modules and versioned entrypoints retain a common implementation while allowing isolated exercise state and independent application deployment.
