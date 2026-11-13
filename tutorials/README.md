# Tutorials

Choose an independently runnable exercise, a guided On-call walkthrough, or a staged implementation lab. Each entry provides its own setup, inputs, editable surface and observable checks.

## Choose a path

| Goal | Entry | Contents |
| --- | --- | --- |
| Explore one mechanism | [Independent exercises](../examples/README.md) | Focused inputs, runtime and observations |
| Observe the integrated On-call workflow | [Screenshot walkthrough](../workshop/capstone/WALKTHROUGH.md) | Services, alerts, investigation, recovery and learning |
| Implement capabilities incrementally | [On-call lab](../workshop/capstone/lab/README.md) | Bounded functions used by the workbench |
| Run the complete reference | [On-call reference](../workshop/capstone/README.md) | Configured application entrypoints and verification commands |

The integrated workflow uses SkillClaw for collective Skill learning. Optimization exercises provide their own datasets and generator settings.

## Reproduce a baseline

[compatibility.json](compatibility.json) records full baseline commits, upstream pins and verification modes. Commands run the current checkout. For an exact baseline, create a separate checkout at the recorded commit and run its setup scripts before opening the exercise.

## Exercise configuration

Each exercise specifies:

- The system commit and upstream dependency revisions.
- Required services, models, credentials and input data.
- Editable files and the interface they implement.
- Expected observations and checks against actual runtime evidence.
- Scripted, replayed, native-protocol or real-model execution mode.
- State isolation, reset and evidence retention.

Checks evaluate the specified behavior against protected fixtures or business observations. Keep reference implementations, editable learner files and evaluator data in their documented locations.

For continuous deployment, see [deployment profiles](../deploy/README.md). For implementation boundaries and planned capabilities, see the [architecture](../docs/architecture/README.md) and [roadmap](../docs/architecture/ROADMAP.md).
