"""Trusted Workbench-to-action-gateway binding; never exposed to model tools."""

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError

from apps.dedicated.client import GatewayClient
from environment import load_json, write_json


class EnterpriseRecovery:
    def __init__(self, config):
        self.tenant, self.executor = config["tenant"], config["executor"]
        if not all(isinstance(value, str) and value.strip() for value in (self.tenant, self.executor)):
            raise ValueError("Dedicated recovery requires tenant and executor identities")
        self.credential_names = {config["approver_token_env"], config["executor_token_env"]}
        self.engineer = GatewayClient(config["gateway_url"], os.environ[config["approver_token_env"]])
        self.worker = GatewayClient(config["gateway_url"], os.environ[config["executor_token_env"]])

    def child_environment(self):
        return {key: value for key, value in os.environ.items()
                if key not in self.credential_names and key != "ONCALL_ACTION_CONFIG"}

    @classmethod
    def from_environment(cls):
        path = os.environ.get("ONCALL_ACTION_CONFIG")
        return cls(load_json(Path(path).expanduser().resolve())) if path else None

    def spec(self, request):
        evidence = {key: request[key] for key in ("source", "scope", "session_id", "report_item_id", "report_sha256", "page_sha256")}
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"tenant": self.tenant, "environment": request["environment_id"], "incident": request["incident_id"],
                "action": "clear_lab_fault", "parameters": {"instance": request["instance"], "service": request["service"],
                "fault_generation": request["fault_generation"]}, "evidence_sha256": digest,
                "capability_sha256": request["skill_sha256"]}

    def approve(self, request, directory):
        spec = self.spec(request)
        path = directory / "enterprise-intent.json"
        if path.exists():
            intent = load_json(path)
            if intent["spec"] != spec or intent["operation"] != request["operation"]:
                raise ValueError("Dedicated recovery intent does not match the current evidence")
            return intent
        approval = self.engineer.approve(self.executor, spec)
        intent = {"operation": request["operation"], "spec": spec, "approval": approval}
        write_json(path, intent)
        return intent

    def execute(self, request, directory):
        intent = self.approve(request, directory)
        try:
            outcome = self.engineer.reconcile(intent["operation"])
        except HTTPError as exc:
            if exc.code != 404:
                raise  # configured gateway failures never fall back to local recovery
            if intent["approval"]["body"]["expires_at"] <= time.time():
                # This path is reached only by an explicitly confirmed UI action,
                # and the gateway proves no dispatch intent exists for this ID.
                intent["approval"] = self.engineer.approve(self.executor, intent["spec"])
                write_json(directory / "enterprise-intent.json", intent)
            outcome = self.worker.dispatch(intent["operation"], intent["spec"], intent["approval"])
        if outcome["state"] != "completed":
            raise RuntimeError("Dedicated recovery outcome remains unresolved; reconcile before continuing")
        receipt = outcome["receipt"]
        if receipt.get("operation") != request["operation"] or not isinstance(receipt.get("effect"), dict):
            raise ValueError("Dedicated recovery did not return an operation-bound effect")
        return {**receipt["effect"], "authority": "dedicated_action_gateway", "operation": request["operation"]}
