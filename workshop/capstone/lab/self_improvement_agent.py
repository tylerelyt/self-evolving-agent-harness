"""Build the On-call agent's improvement loop one checked step at a time."""


def route_alert(steer, alert):
    """Part 1: deliver the original monitor event to the running Hermes Turn."""
    # TODO: build the ONCALL_ALERT instruction, call steer and return its result.
    return False


def branch_report(sessions, record):
    """Part 2: return the child Session ID and the source-to-copy Item mapping."""
    # TODO: use the source Session and report Item recorded by the patrol.
    raise NotImplementedError("Part 2: fork the saved incident report")


def notify_operator(path, recipient, scope, result, evidence):
    """Part 3: save a page receipt that points to the branch and its report."""
    # TODO: write a local inbox receipt from the verified investigation result.
    raise NotImplementedError("Part 3: deliver the investigation to the operator")


def release_policy(results, baseline_sha, candidate_sha):
    """Part 4: decide from already verified paired replay results."""
    # TODO: require comparable evidence, all candidate passes and a strict gain.
    raise NotImplementedError("Part 4: decide whether to release this Skill")
