"""Evidence-based comparison policy; no workload or model dependencies."""


def release_allowed(results, baseline_sha, candidate_sha):
    """Require complete valid evidence, no candidate failures and a repaired case."""
    return bool(
        baseline_sha != candidate_sha
        and results
        and all(row["baseline"]["valid_trace"] is True
                and row["candidate"]["valid_trace"] is True
                and row["candidate"]["passed"] is True for row in results)
        and any(row["baseline"]["passed"] is False for row in results)
    )
