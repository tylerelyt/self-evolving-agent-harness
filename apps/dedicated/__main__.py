"""Run a local authenticated tool gateway with operator-owned tool commands."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from self_evolving_harness.authority import ActionJournal, ApprovalAuthority, Principal


class ToolGateway:
    def __init__(self, config_path):
        path = Path(config_path).resolve()
        value = json.loads(path.read_text())
        if value.get("schema_version") != 1:
            raise ValueError("Unsupported gateway configuration")
        key = os.environ[value["signing_key_env"]].encode()
        self.journal = ActionJournal(path.parent / value["journal"], ApprovalAuthority(key))
        self.principals, self.tokens = {}, {}
        for item in value["principals"]:
            principal = Principal(item["name"], frozenset(item["roles"]),
                                  frozenset(tuple(scope) for scope in item["grants"]))
            if principal.name in self.principals or any(len(scope) != 3 for scope in principal.grants):
                raise ValueError("Duplicate principal or invalid grant")
            token = os.environ[item["token_env"]]
            if len(token) < 32 or token in self.tokens.values():
                raise ValueError("Principal credentials must be unique private values of at least 32 characters")
            self.principals[principal.name], self.tokens[principal.name] = principal, token
        self.tools = value["tools"]
        for tool in self.tools.values():
            for key in ("apply", "observe"):
                argv = tool[key]
                if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
                    raise ValueError("Tools must use operator-owned argument arrays")
                if not Path(argv[0]).is_absolute():
                    raise ValueError("Tool executables must have explicit absolute paths")
        self.timeout = value.get("tool_timeout", 30)
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 120:
            raise ValueError("Tool timeout must be within (0,120]")
        self.cwd = path.parent

    def authenticate(self, header):
        supplied = header.removeprefix("Bearer ") if header and header.startswith("Bearer ") else ""
        for name, token in self.tokens.items():
            if hmac.compare_digest(supplied, token):
                return self.principals[name]
        raise PermissionError("Authentication required")

    def tool(self, kind, operation, spec):
        # Only configured action names select executables. JSON arguments go on stdin.
        command = self.tools[spec["action"]][kind]
        payload = json.dumps({"operation": operation, "spec": spec})
        env = {k: v for k, v in os.environ.items()
               if k in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}}
        result = subprocess.run(command, input=payload, text=True, capture_output=True,
                                timeout=self.timeout, cwd=self.cwd, env=env)
        if result.returncode:
            raise RuntimeError("Tool outcome unknown; reconcile against external state")
        return json.loads(result.stdout)

    def handle(self, route, principal, body):
        if route == "/v1/approvals":
            executor = self.principals[body["executor"]]
            return self.journal.authority.issue(principal, executor, body["spec"], ttl=body.get("ttl", 120))
        if route == "/v1/actions":
            return self.journal.dispatch(body["operation"], principal, body["spec"], body["approval"],
                                         lambda operation, spec: self.tool("apply", operation, spec))
        if route == "/v1/reconcile":
            return self.journal.reconcile(body["operation"], principal,
                                          lambda operation, spec: self.tool("observe", operation, spec))
        raise KeyError(route)


def make_server(gateway, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log credentials or request bodies.

        def do_POST(self):
            try:
                principal = gateway.authenticate(self.headers.get("Authorization"))
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise ValueError("Request body must be within 64 KiB")
                body = json.loads(self.rfile.read(size))
                result, status = gateway.handle(self.path, principal, body), 200
            except PermissionError:
                result, status = {"error": "unauthorized"}, 403
            except KeyError:
                result, status = {"error": "not_found"}, 404
            except (ValueError, TypeError):
                result, status = {"error": "invalid_request"}, 400
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                result, status = {"error": "outcome_unknown_reconcile_required"}, 409
            payload = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with make_server(ToolGateway(args.config), args.port) as server:
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
