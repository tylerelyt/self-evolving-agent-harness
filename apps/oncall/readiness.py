"""Describe implementation readiness separately from verified workshop checks."""

import ast
import inspect
import math
import textwrap


STAGES = {
    1: {"name": "patrol", "title": "巡检上线", "functions": ("patrol_config",)},
    2: {"name": "oncall", "title": "告警与人工交接",
        "functions": ("route_alert", "branch_report", "notify_operator")},
    3: {"name": "memory", "title": "跨会话记忆", "functions": ("recall_memory",)},
    4: {"name": "learning", "title": "独立评测与放行", "functions": ("release_policy",)},
}


def implemented(function):
    if not callable(function):
        return False
    try:
        node = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    except (OSError, TypeError, SyntaxError):
        return False
    body = [item for item in node.body
            if not isinstance(item, ast.Expr) or not isinstance(item.value, ast.Constant)
            or not isinstance(item.value.value, str)]
    if not body or all(isinstance(item, ast.Pass) for item in body):
        return False
    if any(isinstance(item, ast.Raise) for item in body):
        return False
    if len(body) == 1 and isinstance(body[0], ast.Return):
        value = body[0].value
        if value is None or isinstance(value, ast.Constant) and value.value in (False, None):
            return False
    return True


def implemented_stages(implementation):
    """Report edited stubs. Only check.json establishes verified capability."""
    return {number: all(implemented(getattr(implementation, name, None)) for name in stage["functions"])
            for number, stage in STAGES.items()}


def stage_status(path):
    from implementation import load_implementation

    implementation = load_implementation(path)
    readiness = implemented_stages(implementation)
    return [{"number": number, "name": stage["name"], "title": stage["title"],
             "implemented": readiness[number]}
            for number, stage in STAGES.items()]


def read_patrol_config(implementation):
    from environment import SERVICES

    config = implementation.patrol_config()
    if not isinstance(config, dict) or config.keys() != {"services", "sample", "interval"}:
        raise ValueError("patrol_config must return services, sample and interval")
    services = config["services"]
    if (not isinstance(services, list) or not services or len(services) != len(set(services))
            or any(service not in SERVICES for service in services)):
        raise ValueError("Choose distinct services from orders, payments, inventory and notifications")
    if type(config["sample"]) is not int or not 1 <= config["sample"] <= len(services):
        raise ValueError("sample must be a positive count within the selected services")
    interval = config["interval"]
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be positive and finite")
    return config
