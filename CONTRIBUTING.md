# Contributing

```bash
make setup
make check
```

Change one behavior and run its checks. `make demo` runs the native On-call loop with scripted model responses, including publication, adoption and rollback.

- [Experiments](examples/README.md): each directory owns its inputs and runtime.
- [On-call implementation](workshop/capstone/self_improvement_agent.py): complete reference orchestration.
- [Editable lab](workshop/capstone/lab/self_improvement_agent.py): six hooks checked against the real runtime.
- [Running reference](workshop/capstone/RUNNING.md): model configuration, evidence and integration checks.

Describe the resulting behavior and the command that proves it. Keep dependency revisions pinned; report scripted, native-protocol and online-model verification separately. Runtime files, credentials and private inputs stay outside commits.
