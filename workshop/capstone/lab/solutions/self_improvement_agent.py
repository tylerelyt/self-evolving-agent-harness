"""Reference implementation for the On-call improvement loop."""

from fleet_eval import release_allowed as release_policy
from hermes_runtime import fork as branch_report
from investigation import page_operator as notify_operator
from patrol import steer_oncall as route_alert


def patrol_config():
    return {"services": ["orders", "payments", "inventory", "notifications"], "sample": 2, "interval": 1}


def recall_memory(state, memory, environment_state, scope, options):
    from fact_handoff import handoff

    return handoff(state, memory, environment_state, scope, options)
