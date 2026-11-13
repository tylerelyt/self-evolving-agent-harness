"""Transport client: credentials are supplied by the trusted Harness adapter."""

import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class GatewayClient:
    def __init__(self, base_url, token, *, timeout=130):
        parsed = urlsplit(base_url)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.hostname or not (parsed.scheme == "https" or parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("Gateway URL must use HTTPS or loopback HTTP")
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def post(self, route, body):
        request = Request(self.base_url + route, json.dumps(body).encode(),
                          {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def approve(self, executor, spec, *, ttl=120):
        return self.post("/v1/approvals", {"executor": executor, "spec": spec, "ttl": ttl})

    def dispatch(self, operation, spec, approval):
        return self.post("/v1/actions", {"operation": operation, "spec": spec, "approval": approval})

    def reconcile(self, operation):
        return self.post("/v1/reconcile", {"operation": operation})
