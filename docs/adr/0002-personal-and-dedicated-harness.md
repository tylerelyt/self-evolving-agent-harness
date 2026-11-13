# ADR 0002: Personal and dedicated Harness deployments

Date: 2026-11-13

Status: accepted.

## Decision

Provide personal and dedicated workload profiles with persistent execution and learning state. Both use the common candidate, evaluation, publication and adoption lifecycle. On-call is the maintained service-inspection workload.

A dedicated adapter supplies its context, tools, identity integration, approvals and task evaluation. It can use a different execution process through the deployment profile's command binding.

Personal native learning writes an isolated learner workspace. Governed tasks use evaluated, immutable Skill assignments. Enterprise tool execution uses authenticated scope grants, signed approvals and an action journal through the dedicated gateway.

## Consequences

The POSIX deployment supervisor owns foreground processes, workspace locks, shutdown and lifecycle records. Tool authorization belongs to the gateway and workload adapters. Operating-system isolation requires separate identities or containers; organization SSO, distributed ownership and arbitrary external-side-effect recovery require additional integration.

Process liveness, application readiness and task quality are reported separately. Exercise state uses separate workspaces and pinned compatibility baselines.
