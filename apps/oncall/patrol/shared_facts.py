"""Publish verified incident facts and recall them through Hermes on demand."""

from contextlib import closing
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

from environment import SERVICES
from hermes_runtime import SOURCE


FACT_FIELDS = frozenset({
    "incident_id", "instance", "environment_id", "service", "source_session_id",
    "source_report_item_id", "investigation_session_id", "investigation_report_item_id",
    "probe_statuses", "recovery_end", "source_skill_sha256",
})
LOOKUP_TOOL = "memory_lookup"
SERVICE_PATTERN = "|".join(SERVICES)


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_scope(incident_id, service, environment_id):
    if (service not in SERVICES or not matches(r"[0-9a-f]{32}", environment_id)
            or not matches(rf"lab-(?:{SERVICE_PATTERN})-[0-9a-f]{{12}}", incident_id)):
        raise ValueError("Invalid historical incident scope")


def validate_fact(fact):
    if not isinstance(fact, dict) or fact.keys() != FACT_FIELDS:
        raise ValueError("Historical fact must use only the verified incident fields")
    service = fact["service"]
    validate_scope(fact["incident_id"], service, fact["environment_id"])
    if (not fact["incident_id"].startswith(f"lab-{service}-")
            or not matches(r"instance-[a-z0-9]+", fact["instance"])
            or not matches(rf"patrol-{service}-[0-9a-f]{{32}}", fact["source_session_id"])
            or not matches(r"patrol-fork-[0-9a-f]{32}", fact["investigation_session_id"])
            or not matches(r"[0-9a-f]{64}", fact["source_skill_sha256"])
            or any(type(fact[key]) is not int or fact[key] < 1
                   for key in ("source_report_item_id", "investigation_report_item_id"))):
        raise ValueError("Invalid historical fact provenance")

    probes = fact["probe_statuses"]
    recovery_end = fact["recovery_end"]
    if (not isinstance(probes, list) or len(probes) != 3
            or any(type(status) is not int or status // 100 not in (2, 5) for status in probes)
            or not any(status >= 500 for status in probes)
            or type(recovery_end) not in (int, float) or not math.isfinite(recovery_end) or recovery_end <= 0):
        raise ValueError("Invalid historical HTTP evidence")


def publish_fact(path, fact):
    """Commit an approved fact once and reject attempts to rewrite its history."""
    validate_fact(fact)
    body = json.dumps({**fact, "recovery_end": float(fact["recovery_end"])}, sort_keys=True, allow_nan=False)
    scope = (fact["incident_id"], fact["service"], fact["environment_id"])
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                incident_id TEXT PRIMARY KEY, service TEXT NOT NULL,
                environment_id TEXT NOT NULL, body TEXT NOT NULL
            )
        """)
        db.execute("INSERT INTO facts VALUES (?, ?, ?, ?) ON CONFLICT(incident_id) DO NOTHING", (*scope, body))
        saved = db.execute("SELECT service, environment_id, body FROM facts WHERE incident_id = ?",
                           scope[:1]).fetchone()
        if saved != (*scope[1:], body):
            raise ValueError("Incident ID is already bound to a different historical fact")


def read_fact(path, scope):
    try:
        path.stat()
    except FileNotFoundError:
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT body FROM facts WHERE incident_id = ? AND service = ? AND environment_id = ?",
                         scope).fetchone()
    if row is None:
        return None
    fact = json.loads(row[0])
    validate_fact(fact)
    if (fact["incident_id"], fact["service"], fact["environment_id"]) != scope:
        raise ValueError("Historical fact body does not match the requested scope")
    return fact


def make_memory_lookup(path, incident_id, service, environment_id, *, session_id=""):
    """Bind one exact scope to a read-only, explicitly invoked provider tool."""
    validate_scope(incident_id, service, environment_id)
    path = Path(path).resolve()
    scope = (incident_id, service, environment_id)
    if not (SOURCE / "agent/memory_manager.py").is_file():
        raise RuntimeError("Install the pinned course dependencies with bash scripts/setup_deps.sh")
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider

    class SharedFactProvider(MemoryProvider):
        name = "capstone-shared-facts"
        initialized = False

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            self.initialized = True

        def get_tool_schemas(self):
            return [{"name": LOOKUP_TOOL, "description": "Read the scoped historical incident fact.",
                     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        def handle_tool_call(self, tool_name, args, **kwargs):
            if not self.initialized or tool_name != LOOKUP_TOOL or args != {}:
                raise ValueError("Historical memory only permits a lookup in the assigned scope")
            return json.dumps({"fact": read_fact(path, scope), "historical_only": True})

        def shutdown(self):
            self.initialized = False

    def lookup():
        manager = MemoryManager()
        manager.add_provider(SharedFactProvider())
        try:
            manager.initialize_all(session_id=session_id, hermes_home=str(path.parent))
            result = json.loads(manager.handle_tool_call(LOOKUP_TOOL, {}))
            if "error" in result:
                raise RuntimeError(result["error"])
            return result
        finally:
            manager.shutdown_all()

    return lookup
