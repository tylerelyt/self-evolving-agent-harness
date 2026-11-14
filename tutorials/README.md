# Tutorials

Run `make setup` from the repo root, then choose an entry. Each exercise owns its inputs and runtime.

| Do this | Open |
| --- | --- |
| Run the complete native On-call loop | `make demo` · [results and code](../workshop/capstone/README.md) |
| Change six functions and observe the system | `make lab` · [implementation](../workshop/capstone/lab/self_improvement_agent.py) |
| Inspect one mechanism | [23 independent experiments and keywords](../examples/README.md) |
| Follow the UI | [screenshot walkthrough](../workshop/capstone/WALKTHROUGH.md) |

`make demo` uses real Hermes, SkillClaw and local HTTP with scripted model responses. Live-model quality requires its own evaluation. [Versions and upstream pins](compatibility.json) · [continuous deployment](../deploy/README.md).
