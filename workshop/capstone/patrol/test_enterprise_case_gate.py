"""Focused checks for scoring rules using small, constructed snapshots."""

import json
import unittest

from enterprise_case_gate import grade
from enterprise_replay import lesson_module, monitor_lookup


CLUSTER = "instance-a"
FRONT = "payments"
ROOT = "inventory"
DECOY = "orders"
START = "2026-11-09T12:00:00Z"
TIME = "2026-11-09T12:00:01Z"
END = "2026-11-09T12:00:03Z"


def fixture(scenario):
    concurrent = scenario == "concurrent_incident"
    target = [("alerts", FRONT), ("dependencies", FRONT), ("metrics", ROOT)]
    target += [("traces", FRONT), ("logs", ROOT)] if concurrent else []
    foreign = [("alerts", DECOY), ("logs", DECOY)] if concurrent else []
    missing = [] if concurrent else [("traces", FRONT), ("logs", ROOT)]
    rows = [
        {"id": f"record-{i}", "incident_id": "target" if i < len(target) else "foreign",
         "kind": kind, "cluster": CLUSTER, "service": service, "time": TIME, "value": {}}
        for i, (kind, service) in enumerate(target + foreign)
    ]
    required = [row["id"] for row in rows if row["incident_id"] == "target"]
    forbidden = [row["id"] for row in rows if row["incident_id"] == "foreign"]
    needed = [{"kind": kind, "cluster": CLUSTER, "service": service} for kind, service in missing]
    queries = [{"kind": kind, "cluster": CLUSTER, "service": service, "status": "covered"}
               for kind, service in target + foreign]
    queries.extend({**selector, "status": "not_covered"} for selector in needed)
    expected = {"root_cause": f"{CLUSTER}/{ROOT}" if concurrent else None,
                "affected_services": [f"{CLUSTER}/{FRONT}"],
                "excluded_services": [f"{CLUSTER}/{DECOY}"] if concurrent else [],
                "needed_evidence": needed}
    snapshot = {"incident_id": "target", "snapshot_id": scenario, "version": "test", "rows": rows,
                "coverage": {"start": START, "end": END, "complete": True},
                "manifest": {"clusters": {CLUSTER: sorted({row["kind"] for row in rows})}}}
    scope = {"cluster": CLUSTER, "service": FRONT, "start": START, "end": END}
    case = {"scenario": scenario, "snapshot": snapshot,
            "input": {"prompt": "CAPSTONE_SCOPE " + json.dumps(scope) + "\nCAPSTONE_INCIDENT target"},
            "annotation": {"expected_output": expected, "required_evidence": required,
                           "forbidden_evidence": forbidden, "required_queries": queries}}
    monitor = lesson_module("monitor")
    receipts = [monitor.query(snapshot, query["kind"], CLUSTER, START, END, query["service"]) for query in queries]
    answer = {**expected, "evidence_ids": list(required), "explanation": "测试说明"}
    return case, receipts, answer


class BusinessCaseGateTests(unittest.TestCase):
    def result(self, case, receipts, answer):
        return grade(case, {"status": "completed", "receipts": receipts, "final_message": json.dumps(answer)})

    def test_distinguishes_independent_same_cluster_incident(self):
        case, receipts, answer = fixture("concurrent_incident")
        self.assertTrue(self.result(case, receipts, answer)["passed"])
        answer["evidence_ids"].append(case["annotation"]["forbidden_evidence"][-1])
        self.assertEqual(self.result(case, receipts, answer)["score"], 0)

    def test_broad_or_empty_exact_query_is_not_scoped_distractor_evidence(self):
        case, receipts, answer = fixture("concurrent_incident")
        query = lesson_module("monitor").query
        broad = query(case["snapshot"], "logs", CLUSTER, START, END)
        empty = query(case["snapshot"], "logs", CLUSTER, END, END, DECOY)
        result = self.result(case, [*receipts[:-1], broad, empty], answer)
        self.assertEqual((result["status"], result["score"]), ("scored", 0))

    def test_abstention_requires_null_root_followup_and_real_in_window_receipt(self):
        case, receipts, answer = fixture("insufficient_coverage")
        self.assertTrue(self.result(case, receipts, answer)["passed"])
        self.assertEqual(self.result(case, receipts, {**answer, "root_cause": f"{CLUSTER}/{ROOT}"})["score"], 0)
        self.assertEqual(self.result(case, receipts, {**answer, "needed_evidence": []})["score"], 0)
        query = lesson_module("monitor").query
        outside = query(case["snapshot"], "logs", CLUSTER, "2026-11-09T11:59:59Z", END, ROOT)
        self.assertEqual(outside["status"], "not_covered")
        self.assertEqual(self.result(case, [*receipts[:-1], outside], answer)["score"], 0)

    def test_forged_receipt_bad_annotation_and_infrastructure_are_unscored(self):
        case, receipts, answer = fixture("concurrent_incident")
        forged = {**receipts[-1], "rows": []}
        self.assertIsNone(self.result(case, [*receipts[:-1], forged], answer)["score"])
        case["annotation"]["required_evidence"].extend(case["annotation"]["forbidden_evidence"])
        self.assertIsNone(self.result(case, receipts, answer)["score"])
        self.assertIsNone(grade(case, {"status": "infrastructure_error"})["score"])

    def test_model_query_error_cannot_substitute_for_evidence(self):
        case, receipts, answer = fixture("insufficient_coverage")
        request = {**receipts[-1]["query"], "kind": "unsupported"}
        invalid = monitor_lookup(lesson_module("monitor").query, case["snapshot"], request)
        self.assertTrue(self.result(case, [*receipts, invalid], answer)["passed"])
        self.assertEqual(self.result(case, [*receipts[:-1], invalid], answer)["score"], 0)
        mutated = {**invalid, "error": "not_covered"}
        self.assertIsNone(self.result(case, [*receipts, mutated], answer)["score"])

    def test_public_incident_must_match_snapshot(self):
        case, receipts, answer = fixture("insufficient_coverage")
        case["input"]["prompt"] = case["input"]["prompt"].replace("CAPSTONE_INCIDENT target", "CAPSTONE_INCIDENT foreign")
        self.assertIsNone(self.result(case, receipts, answer)["score"])


if __name__ == "__main__":
    unittest.main()
