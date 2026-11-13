"""Operator-scoped local HTTP recovery tool for the dedicated action gateway."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from apps.oncall.paths import prepare
prepare()
from environment import _append_change, change_lock, load_json, write_json
from fleet import clear_lab_fault, scoped_observation
from self_evolving_harness.authority import validate_spec


def execute(config_path, kind, operation, spec):
    spec = validate_spec(spec)
    config_path = Path(config_path).resolve()
    config = load_json(config_path)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation):
        raise ValueError("Invalid operation identity")
    parameters = spec["parameters"]
    if spec["action"] != "clear_lab_fault" or set(parameters) != {"instance", "service", "fault_generation"}:
        raise PermissionError("Unsupported local recovery action")
    scope = next((item for item in config["environments"] if
                  (item["tenant"], item["environment_id"], item["instance"]) ==
                  (spec["tenant"], spec["environment"], parameters["instance"]) and
                  parameters["service"] in item["services"]), None)
    if scope is None:
        raise PermissionError("Tool is not authorized for this environment or service")
    state = (config_path.parent / scope["state"]).resolve()
    receipts = (config_path.parent / config["receipts"]).resolve()
    receipts.mkdir(parents=True, exist_ok=True)
    path = receipts / (operation + ".json")
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with change_lock(state):
        # Read actual HTTP, not the caller's environment or a request-supplied path.
        scoped_observation(state, parameters["service"], spec["environment"])
        if path.exists():
            receipt = load_json(path)
            if receipt["spec_sha256"] != digest:
                raise PermissionError("Operation already belongs to a different scope")
            return receipt
        if kind == "observe":
            return {"operation": operation, "state": "unknown", "spec_sha256": digest}
        flag = state / (parameters["service"] + ".fault")
        if not flag.is_file():
            raise RuntimeError("No generation-matched fault can be attributed to this operation")
        marker = flag.stat()
        if {"inode": marker.st_ino, "mtime_ns": marker.st_mtime_ns} != parameters["fault_generation"]:
            raise PermissionError("Approval cannot clear a newly injected fault generation")
        before = "intermittent" if flag.read_text() == "intermittent" else "on"
        effect = clear_lab_fault(state, parameters["service"], spec["environment"])
        _append_change(state, {"type": "fault_changed", "environment_id": spec["environment"],
                              "service": parameters["service"], "before": before, "after": "off",
                              "actor": "dedicated_action_gateway", "operation": operation,
                              "incident_id": spec["incident"], "summary": "Scoped gateway approval applied"})
        receipt = {"operation": operation, "state": "completed", "spec_sha256": digest, "effect": effect}
        write_json(path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("apply", "observe"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    request = json.load(sys.stdin)
    print(json.dumps(execute(args.config, args.mode, request["operation"], request["spec"])))


if __name__ == "__main__":
    main()
