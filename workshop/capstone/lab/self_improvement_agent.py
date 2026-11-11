"""Bring your own On-call agent online, one workshop stage at a time."""


def patrol_config():
    """Stage 1: choose services, the sample size and the interval in seconds."""
    # Return {"services": [...], "sample": ..., "interval": ...}.
    # The prepared environment provides orders, payments, inventory, notifications.
    raise NotImplementedError("Stage 1: configure the recurring Hermes patrol")


def route_alert(steer, alert):
    """Stage 2 / legacy part 1: deliver the original event to the running Turn."""
    # TODO: build the ONCALL_ALERT instruction, call steer and return its result.
    return False


def branch_report(sessions, record):
    """Stage 2 / legacy part 2: fork the report and return its Item mapping."""
    # TODO: use the source Session and report Item recorded by the patrol.
    raise NotImplementedError("Part 2: fork the saved incident report")


def notify_operator(path, recipient, scope, result, evidence):
    """Stage 2 / legacy part 3: page the engineer with the branch and report."""
    # TODO: write a local inbox receipt from the verified investigation result.
    raise NotImplementedError("Part 3: deliver the investigation to the operator")


def recall_memory(state, memory, environment_state, scope, options):
    """Stage 3: recall a verified incident in a new Hermes session."""
    # state is the new session's directory, memory is the shared SQLite path,
    # environment_state points to the live HTTP environment, and scope contains
    # incident_id/service/environment_id. options contains the model connection.
    # Query the historical fact, then obtain fresh HTTP metrics. Return the
    # session and ordered evidence produced by fact_handoff.handoff.
    raise NotImplementedError("Stage 3: connect historical memory to a new investigation")


def release_policy(results, baseline_sha, candidate_sha):
    """Stage 4 / legacy part 4: decide from verified paired replay results."""
    # TODO: require comparable evidence, all candidate passes and a strict gain.
    raise NotImplementedError("Part 4: decide whether to release this Skill")
