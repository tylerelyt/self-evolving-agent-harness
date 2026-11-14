"""Load the same On-call implementation in checks and runtime processes."""

from importlib.util import module_from_spec, spec_from_file_location
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sys
import time


CAPSTONE = Path(__file__).resolve().parent
REFERENCE = CAPSTONE / "self_improvement_agent.py"

HOOKS = ("patrol_config", "route_alert", "branch_report", "notify_operator", "recall_memory", "release_policy")


def bind_implementation(path, directory):
    """Freeze the selected hook module for one reproducible end-to-end run."""
    path, directory = Path(path).resolve(), Path(directory).resolve()
    content = path.read_bytes()
    directory.mkdir(parents=True, exist_ok=False)
    snapshot = directory / "selected.py"
    snapshot.write_bytes(content)
    binding = {"source_name": path.name, "sha256": hashlib.sha256(content).hexdigest(),
               "snapshot": "implementation/selected.py", "calls": "implementation/calls.jsonl"}
    (directory / "binding.json").write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
    return snapshot, binding


def _track_hooks(module, path, binding):
    def tracked(name, function):
        @wraps(function)
        def call(*args, **kwargs):
            if hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
                raise RuntimeError("The bound On-call implementation changed during execution")
            def record(status):
                event = {"hook": name, "status": status, "sha256": binding["sha256"],
                         "pid": os.getpid(), "at": time.time()}
                line = (json.dumps(event) + "\n").encode("utf-8")
                descriptor = os.open(path.parent / "calls.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                try:
                    if os.write(descriptor, line) != len(line):
                        raise OSError("Incomplete implementation call receipt")
                finally:
                    os.close(descriptor)
            record("started")
            try:
                result = function(*args, **kwargs)
            except BaseException:
                record("failed")
                raise
            record("finished")
            return result
        return call
    for name in HOOKS:
        function = getattr(module, name, None)
        if not callable(function):
            raise ValueError(f"The On-call implementation is missing {name}")
        setattr(module, name, tracked(name, function))
    return module


def implementation_evidence(path, binding):
    """Report actual hook execution separately from successful business outcomes."""
    path = Path(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
        raise RuntimeError("The bound On-call implementation changed during execution")
    log = path.parent / "calls.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    if any(event["sha256"] != binding["sha256"] or event["hook"] not in HOOKS for event in events):
        raise RuntimeError("Implementation call receipts do not match the selected source")
    finished = sorted({event["hook"] for event in events if event["status"] == "finished"})
    return {**binding, "executed_hooks": finished, "missing_hooks": sorted(set(HOOKS) - set(finished)),
            "failed_calls": sum(event["status"] == "failed" for event in events),
            "all_hooks_executed": set(finished) == set(HOOKS)}


def verify_stage_check(path, source_sha256, *, live):
    """Bind the final rehearsal to a cumulative check of the same source."""
    raw = Path(path).read_bytes()
    check = json.loads(raw)
    stages = check.get("checks", [])
    if (check.get("schema") != "oncall-workshop-check-v1" or check.get("status") != "passed"
            or check.get("stage") != 4 or check.get("mode") != ("live" if live else "local-script-model")
            or check.get("implementation", {}).get("sha256") != source_sha256
            or [item.get("stage") for item in stages] != [1, 2, 3, 4]
            or any(item.get("status") != "passed" for item in stages)):
        raise ValueError("Run stage 4 again with the same implementation and model mode before the final rehearsal")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "implementation_sha256": source_sha256,
            "stage": 4, "mode": check["mode"], "status": "passed"}


def load_implementation(path):
    path = Path(path).resolve()
    binding_file = path.parent / "binding.json"
    binding = None
    if path.name == "selected.py" and binding_file.is_file():
        binding = json.loads(binding_file.read_text(encoding="utf-8"))
        if hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
            raise RuntimeError("The bound On-call implementation changed during execution")
    spec = spec_from_file_location("oncall_service_implementation", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load the On-call implementation from {path}")
    if str(CAPSTONE) not in sys.path:
        sys.path.insert(0, str(CAPSTONE))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if binding is not None:
        return _track_hooks(module, path, binding)
    return module
