"""Focused checks for the shared root scorer using a constructed snapshot."""

from copy import deepcopy
import json
import unittest

from enterprise_pack import iso, row
from enterprise_replay import lesson_module
from fanout_gate import SCENARIO, grade, validate_case


CLUSTER, ROOT, CONTROL = "lab", "inventory", "notifications"
DOWNSTREAMS = ("payments", "orders")
START, END = iso(100), iso(112)


def fixture():
    def evidence(identifier, kind, service, time, value, incident="fanout"):
        return {**row(identifier, kind, CLUSTER, service, time, value), "incident_id": incident}

    def metrics(service, error_rate):
        return {"service": service, "environment_id": "fanout-env", "source": "live_lab_http_requests",
                "window_start": 101, "window_end": 112, "requests": 20, "error_rate": error_rate, "p95_ms": 42}

    rows = [evidence("root-metric", "metrics", ROOT, 112, metrics(ROOT, .8)),
            evidence("control-metric", "metrics", CONTROL, 112, metrics(CONTROL, 0), "control")]
    for index, service in enumerate(DOWNSTREAMS, start=1):
        time, trace = 103 + index * 2, f"{index:032x}"
        rows.extend([
            evidence(f"alert-{index}", "alerts", service, 112, metrics(service, .8)),
            evidence(f"span-{index}", "traces", service, time + 1,
                     {"trace_id": trace, "started_at": time - 1, "time": time + 1, "service": service,
                      "upstream_service": ROOT, "upstream": f"{CLUSTER}/{ROOT}", "status": 503, "upstream_status": 503}),
            evidence(f"log-{index}", "logs", ROOT, time,
                     {"trace_id": trace, "time": time, "service": ROOT, "caller_service": service,
                      "status": 503, "fault_scope": ROOT}),
            evidence(f"dep-{index}", "dependencies", service, time + 1,
                     {"upstream": [f"{CLUSTER}/{ROOT}"], "trace_id": trace}),
        ])
    required = [item["id"] for item in rows if item["incident_id"] == "fanout"]
    scopes = sorted({(item["kind"], item["service"]) for item in rows})
    queries = [{"kind": kind, "cluster": CLUSTER, "service": service, "status": "covered"} for kind, service in scopes]
    expected = {"root_cause": f"{CLUSTER}/{ROOT}", "affected_services": [f"{CLUSTER}/{name}" for name in DOWNSTREAMS],
                "excluded_services": [f"{CLUSTER}/{CONTROL}"], "needed_evidence": []}
    snapshot = {"incident_id": "fanout", "snapshot_id": "shared-root", "version": "test", "rows": rows,
                "coverage": {"start": START, "end": END, "complete": True},
                "manifest": {"clusters": {CLUSTER: sorted({kind for kind, _ in scopes})}}}
    scope = {"cluster": CLUSTER, "service": DOWNSTREAMS[0], "start": START, "end": END}
    case = {"scenario": SCENARIO, "snapshot": snapshot,
            "input": {"prompt": "CAPSTONE_SCOPE " + json.dumps(scope) + "\nCAPSTONE_INCIDENT fanout"},
            "annotation": {"expected_output": expected, "required_evidence": required,
                           "control_evidence": ["control-metric"], "required_queries": queries}}
    query = lesson_module("monitor").query
    receipts = [query(snapshot, kind, CLUSTER, START, END, service) for kind, service in scopes]
    answer = {**expected, "evidence_ids": list(required), "explanation": "测试说明"}
    return case, receipts, answer


class FanoutGateTests(unittest.TestCase):
    def result(self, case, receipts, answer):
        return grade(case, {"status": "completed", "receipts": receipts, "final_message": json.dumps(answer)})

    def test_exact_two_impacts_and_control_exclusion(self):
        case, receipts, answer = fixture()
        self.assertTrue(self.result(case, receipts, answer)["passed"])
        for override in (
            {"affected_services": answer["affected_services"][:1]},
            {"affected_services": answer["affected_services"] + [f"{CLUSTER}/{CONTROL}"]},
            {"excluded_services": []},
            {"evidence_ids": answer["evidence_ids"] + ["control-metric"]},
        ):
            with self.subTest(override=override):
                self.assertEqual(self.result(case, receipts, {**answer, **override})["score"], 0)

    def test_both_upstream_logs_must_be_queried_together_and_control_scoped(self):
        case, receipts, answer = fixture()
        query = lesson_module("monitor").query
        no_root_logs = [item for item in receipts if item["query"]["kind"] != "logs"]
        split_logs = [query(case["snapshot"], "logs", CLUSTER, start, end, ROOT)
                      for start, end in ((START, iso(106)), (iso(106), END))]
        self.assertEqual(self.result(case, no_root_logs + split_logs, answer)["score"], 0)
        no_control = [item for item in receipts if item["query"]["service"] != CONTROL]
        broad = query(case["snapshot"], "metrics", CLUSTER, START, END)
        self.assertEqual(self.result(case, [*no_control, broad], answer)["score"], 0)

    def test_incorrect_provenance_and_forged_receipts_are_unscored(self):
        case, receipts, answer = fixture()
        for identifier, edits, message in (
            ("log-2", {"caller_service": DOWNSTREAMS[0]}, "Upstream log"),
            ("span-2", {"trace_id": "1".zfill(32)}, "distinct failing trace"),
            ("control-metric", {"error_rate": .01}, "not healthy"),
            ("control-metric", {"window_start": 110}, "healthy control window"),
        ):
            damaged = deepcopy(case)
            next(row for row in damaged["snapshot"]["rows"] if row["id"] == identifier)["value"].update(edits)
            with self.subTest(identifier=identifier, edits=edits), self.assertRaisesRegex(ValueError, message):
                validate_case(damaged)
        forged = {**receipts[0], "rows": []}
        self.assertIsNone(self.result(case, [forged, *receipts[1:]], answer)["score"])
        self.assertIsNone(grade(case, {"status": "infrastructure_error"})["score"])


if __name__ == "__main__":
    unittest.main()
