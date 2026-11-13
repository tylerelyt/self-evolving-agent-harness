"""Fixed HTTP replay inputs for search, release review and final holdout."""

from hashlib import sha256
import json

from fleet_eval import CASES, ReplayCase


TRAIN = (
    ReplayCase("gepa-train-01", (502, 200), "intermittent",
               service="orders", environment_id="replay-train-orders",
               window_start=1_700_000_100, requests=40, error_rate=.15, p95_ms=180),
    ReplayCase("gepa-train-02", (500, 502), "persistent",
               service="inventory", environment_id="replay-train-inventory",
               window_start=1_700_000_130, requests=60, error_rate=.10, p95_ms=240),
)
VALIDATION = (
    ReplayCase("gepa-validation-01", (504, 204), "intermittent",
               service="notifications", environment_id="replay-validation-notifications",
               window_start=1_700_000_200, requests=64, error_rate=.03125, p95_ms=680),
    ReplayCase("gepa-validation-02", (502, 504), "persistent",
               service="orders", environment_id="replay-validation-orders",
               window_start=1_700_000_230, requests=32, error_rate=.375, p95_ms=170),
)
HOLDOUT = (
    ReplayCase("gepa-holdout-01", (500, 200), "intermittent",
               service="payments", environment_id="replay-holdout-payments",
               window_start=1_700_000_300, requests=50, error_rate=.16, p95_ms=430),
    ReplayCase("gepa-holdout-02", (504, 503), "persistent",
               service="inventory", environment_id="replay-holdout-inventory",
               window_start=1_700_000_330, requests=35, error_rate=.20, p95_ms=540),
    ReplayCase("gepa-holdout-03", (204,), "not_reproduced",
               service="notifications", environment_id="replay-holdout-notifications",
               window_start=1_700_000_360, requests=24, error_rate=.125, p95_ms=520),
)


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def split_manifest():
    """Record and check separation before the optimizer sees any cases."""
    splits = {}
    cases_seen = set()
    fixtures_seen = set()
    evidence_roles = {}
    for role, cases in (("train", TRAIN), ("validation", VALIDATION),
                        ("release", CASES), ("holdout", HOLDOUT)):
        if not cases:
            raise ValueError(f"The {role} replay split is empty")
        splits[role] = []
        for case in cases:
            fixture = case.fixture()
            snapshot = fixture["snapshot"]
            fixture_sha = fingerprint(fixture)
            if case.name in cases_seen or fixture_sha in fixtures_seen:
                raise ValueError(f"Duplicate replay case or fixture: {case.name}")
            cases_seen.add(case.name)
            fixtures_seen.add(fixture_sha)

            # The fixed release cases share an observation within their own
            # split. Other roles cannot reuse its environment or evidence.
            for evidence in (
                ("environment", snapshot["environment_id"]),
                ("metrics", snapshot["requests"], snapshot["error_rate"], snapshot["p95_ms"]),
                ("probe sequence", case.statuses),
            ):
                other_role = evidence_roles.setdefault(evidence, role)
                if other_role != role:
                    raise ValueError(f"Shared {evidence[0]} between {other_role} and {role}: {case.name}")
            splits[role].append({
                "case": case.name,
                **fixture,
                "snapshot_sha256": fingerprint(snapshot),
                "fixture_sha256": fixture_sha,
            })
    manifest = {"source": "authored_frozen_http_replays", "splits": splits}
    return {**manifest, "manifest_sha256": fingerprint(manifest)}
