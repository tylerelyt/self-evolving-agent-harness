#!/usr/bin/env python3
"""Audit a completed fleet run and its associated evidence without writing files."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

from audit_extensions import audit_extensions
from audit_fleet import audit_core
from export_workset import require, run_file


SCHEMA = "capstone-evidence-audit-v1"


def audit(root, *, langfuse=None, replay=None, fanout=None):
    root = root.resolve(strict=True)
    require(root.is_dir(), "Use a completed fleet run directory")
    report = json.loads(run_file(root, "report.json").read_text(encoding="utf-8"))
    require(report["status"] == "complete", "Fleet run has not completed")
    require(report["mode"] in {"live", "local-script-model"}, "Unknown fleet execution mode")
    checks = {
        "core": audit_core(root, report),
        "extensions": audit_extensions(root, report, langfuse=langfuse, replay=replay),
    }
    if fanout is not None:
        from fanout import audit as audit_fanout

        checks["extensions"]["shared_root_fanout"] = audit_fanout(fanout)
    counts = Counter(check["status"] for group in checks.values() for check in group.values())
    require(counts and counts.keys() <= {"passed", "pending", "not_requested"}, "Invalid audit check status")
    return {
        "schema": SCHEMA,
        "status": "pending" if counts["pending"] else "verified",
        "run": str(root),
        "mode": report["mode"],
        "counts": {status: counts[status] for status in ("passed", "pending", "not_requested")},
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="Completed fleet output directory")
    parser.add_argument("--langfuse", type=Path, help="Langfuse verification or roundtrip output directory")
    parser.add_argument("--replay", type=Path, help="Optional replay output of the restored Langfuse pack")
    parser.add_argument("--fanout", type=Path, help="Optional evidence from the independent shared-root lab")
    parser.add_argument("--require", action="append", default=[], metavar="GROUP.CHECK",
                        help="Require a passed check, such as core.rollback. May be repeated.")
    args = parser.parse_args()
    if args.replay is not None and args.langfuse is None:
        parser.error("--replay requires its associated --langfuse output")
    requirements = {}
    for key in args.require:
        group, _, check = key.partition(".")
        if group not in {"core", "extensions"} or not check or "." in check:
            parser.error(f"Invalid --require check: {key}")
        requirements[key] = (group, check)
    try:
        result = audit(args.run, langfuse=args.langfuse, replay=args.replay, fanout=args.fanout)
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, sqlite3.Error) as exc:
        result = {"schema": SCHEMA, "status": "failed", "run": str(args.run),
                  "error": str(exc) or type(exc).__name__}
    if requirements and result["status"] != "failed":
        result["required_checks"] = {
            key: result["checks"][group].get(check, {"status": "not_requested"})["status"]
            for key, (group, check) in requirements.items()
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "failed":
        raise SystemExit(1)
    if any(status != "passed" for status in result.get("required_checks", {}).values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
