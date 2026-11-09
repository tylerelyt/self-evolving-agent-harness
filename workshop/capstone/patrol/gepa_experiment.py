"""Compare the offline GEPA result without changing the active On-call Skill."""

from hashlib import sha256

from environment import write_json
from fleet_eval import CASES, evaluate
from gepa_cases import HOLDOUT, TRAIN, VALIDATION, split_manifest
from gepa_search import optimize


def compare(root, seed, options, *, live=False):
    root.mkdir()
    report_path = root / "report.json"
    seed_sha = sha256(seed.read_bytes()).hexdigest()
    report = {
        "status": "running",
        "role": "offline_comparison",
        "release_authority": "skillclaw",
        "model": options["model"],
        "reflection_mode": "model" if live else "scripted",
        "seed_sha256": seed_sha,
    }
    try:
        report["splits"] = split_manifest()
        write_json(report_path, report)
        candidate, search = optimize(root / "search", seed, options, TRAIN, VALIDATION, live=live)
        report["search"] = search
        if search["baseline_sha256"] != seed_sha or sha256(seed.read_bytes()).hexdigest() != seed_sha:
            raise RuntimeError("GEPA did not use the frozen seed")
        selected_sha = sha256(candidate.read_bytes()).hexdigest() if candidate is not None else None
        if selected_sha != search["selected_sha256"] or selected_sha == seed_sha:
            raise RuntimeError("GEPA's selection does not match the exported Skill")
        if candidate is None:
            report["status"] = "no_validation_gain"
            return report

        report["selected"] = {"file": str(candidate.relative_to(root)), "sha256": selected_sha}
        write_json(report_path, report)

        # Both gates consume the same validation-selected bytes. Neither result
        # can replace the candidate or feed another GEPA search.
        for role, cases in (("release", CASES), ("holdout", HOLDOUT)):
            evaluation = evaluate(root / role, seed, candidate, options, cases=cases)
            if (evaluation["baseline_sha256"], evaluation["candidate_sha256"]) != (seed_sha, selected_sha):
                raise RuntimeError(f"The {role} replay did not use the frozen Skill versions")
            report[role] = {
                "file": f"{role}/evaluation.json",
                "gate_passed": evaluation["passed"],
                "cases": len(evaluation["cases"]),
                "baseline_passed": sum(case["baseline"]["passed"] for case in evaluation["cases"]),
                "candidate_passed": sum(case["candidate"]["passed"] for case in evaluation["cases"]),
            }
            write_json(report_path, report)
        report["status"] = "completed"
        return report
    except BaseException as exc:
        report.update(status="failed", error=str(exc) or type(exc).__name__)
        raise
    finally:
        write_json(report_path, report)
