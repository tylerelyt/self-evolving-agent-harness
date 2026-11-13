"""Workload-independent coordination of an RSI improvement lifecycle.

Execution/learning adapters supply proposals, evaluators, publishers and verified
adoption receipts. This module has no lesson, fault schedule or application loop.
"""

from hashlib import sha256
from pathlib import Path


class ImprovementCoordinator:
    def __init__(self, ledger):
        self.ledger = ledger

    def _artifact(self, candidate_id, path):
        spec = self.ledger.get(candidate_id)["spec"]
        if sha256(Path(path).read_bytes()).hexdigest() != spec["artifact_sha256"]:
            raise ValueError("Candidate bytes changed after registration")

    def evaluate(self, candidate_id, candidate, evaluator):
        self._artifact(candidate_id, candidate)
        report = evaluator()
        self._artifact(candidate_id, candidate)
        report_digest = self.ledger.evaluate(candidate_id, report)
        return report, report_digest

    def publish(self, candidate_id, candidate, receivers, publisher):
        self._artifact(candidate_id, candidate)
        self.ledger.begin_publication(candidate_id, receivers)
        # A failure intentionally leaves a durable publishing intent. Recovery
        # reconciles actual delivery; calling this again cannot replay it.
        receipt = publisher()
        self._artifact(candidate_id, candidate)
        self.ledger.published(candidate_id, receipt)
        return receipt
