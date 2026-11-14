"""Continuous, evidence-checked incident memory backed by OpenViking.

SessionDB remains the original evidence. KnowledgeStore owns revisions and a
durable publication outbox; retrieval always consults the external provider.
"""

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from uuid import uuid4

from .paths import prepare
prepare()
from environment import SERVICES, load_json, write_json
from fleet import receipt, review_facts
from hermes_runtime import SOURCE, ensure_loopback_no_proxy
from patrol import connect, incidents, model_connection, writer_lock
from workbench_memory import recovered_observation, verified_branch
from self_evolving_harness.control import ControlStore
from self_evolving_harness.knowledge import KnowledgeStore
from self_evolving_harness.learning import canonical

ALIASES = ("instance-a", "instance-b")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bind_configuration(template, root, instance_ids):
    """Bind operator settings to this actual runtime's environment identities."""
    template = Path(template).expanduser().resolve()
    config = load_json(template)
    if config.get("schema_version") != 1 or config.get("backend") != "openviking":
        raise ValueError("Use an explicit schema-1 OpenViking memory configuration")
    if "api_key" in config:
        raise ValueError("Reference an API-key environment variable instead of storing its value")
    tenant = config.get("tenant", "oncall")
    if not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", tenant):
        raise ValueError("Use an explicit safe memory tenant")
    state = (template.parent / config.get("state_dir", "memory")).resolve()
    state.mkdir(parents=True, exist_ok=True)
    identity_path = state / "identity.json"
    identity = {"tenant": tenant, "endpoint": config["endpoint"].rstrip("/"),
                "account": config.get("account", "default"), "user": config.get("user", "default")}
    with writer_lock(state, "identity.sqlite"):
        if identity_path.exists():
            saved = load_json(identity_path)
            if any(saved[k] != v for k, v in identity.items()):
                raise ValueError("Memory storage already belongs to a different deployment binding")
            namespace = saved["namespace"]
            if config.get("namespace", namespace) != namespace:
                raise ValueError("Cannot change the stable memory namespace")
        else:
            namespace = config.get("namespace") or "oncall-" + uuid4().hex
            if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", namespace):
                raise ValueError("Use a safe stable OpenViking namespace")
            write_json(identity_path, {**identity, "namespace": namespace})
    if set(instance_ids) != set(ALIASES) or any(not re.fullmatch(r"[0-9a-f]{32}", value)
                                               for value in instance_ids.values()):
        raise ValueError("Memory must bind both actual service environments")
    services = config.get("services", list(SERVICES))
    if (not isinstance(services, list) or not services or len(set(services)) != len(services)
            or any(service not in SERVICES for service in services)):
        raise ValueError("Memory service scope is invalid")
    for key, default in (("retrieval_timeout", 5), ("publication_timeout", 100), ("fact_ttl_seconds", 2592000)):
        value = config.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Memory timeout/validity settings must be finite and positive")
        config[key] = value
    bound = {**config, **identity, "namespace": namespace, "state_dir": str(state),
             "instances": dict(instance_ids), "services": services, "hermes_source": str(SOURCE),
             "runtime_root": str(Path(root).resolve())}
    provider(bound).root({"tenant": tenant, "environment": instance_ids[ALIASES[0]], "service": services[0]})
    path = Path(root) / "system" / "memory-binding.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, bound)
    return path


def load_binding(path=None):
    selected = path or os.environ.get("ONCALL_MEMORY_CONFIG")
    if not selected:
        return None
    config = load_json(Path(selected).expanduser().resolve())
    if config.get("schema_version") != 1 or config.get("backend") != "openviking":
        raise ValueError("Memory is not bound to OpenViking")
    if not config.get("instances") or not config.get("namespace"):
        raise ValueError("Memory configuration has not been bound to a running environment")
    return config


def provider(config, *, retrieval=False):
    from self_evolving_harness.adapters.openviking import OpenVikingProvider
    return OpenVikingProvider(config["endpoint"], config["namespace"],
        api_key=os.environ.get(config.get("api_key_env", "OPENVIKING_API_KEY"), ""),
        account=config.get("account", "default"), user=config.get("user", "default"),
        actor="oncall-knowledge", hermes_source=config.get("hermes_source", str(SOURCE)),
        timeout=config.get("retrieval_timeout", 5) if retrieval else config.get("publication_timeout", 100),
        retrieval_timeout=config.get("retrieval_timeout", 5))


def configured_lookup(*, root, receiver, service, environment_id, session_id):
    if not os.environ.get("ONCALL_MEMORY_CONFIG"):
        return None

    def lookup():
        try:
            config = load_binding()
            actual = environment_id() if callable(environment_id) else environment_id
            if (receiver not in config["instances"] or actual != config["instances"][receiver]
                    or service not in config["services"]):
                raise PermissionError("Knowledge lookup is outside the assigned service environment")
            scope = {"tenant": config["tenant"], "environment": actual, "service": service}
            result = KnowledgeStore(Path(config["state_dir"]) / "knowledge.sqlite3").search(
                provider(config, retrieval=True), scope=scope, query=f"{service} incident recovery runbook", limit=12)
            facts, runbooks = [], []
            assignment = Path(config["runtime_root"]) / "learning/state.json"
            active_method = load_json(assignment).get("active_sha256") if assignment.is_file() else None
            for hit in result["facts"]:
                revision, citation = hit["revision"], hit["citation"]
                item = {"revision_id": revision["revision_id"], "key": revision["key"],
                        "content": revision["content"], "sources": revision["sources"],
                        "valid_until": revision.get("valid_until"), **citation}
                if revision["key"].startswith("runbook:"):
                    if revision["content"].get("sha256") == active_method:
                        runbooks.append(item)
                else:
                    facts.append(item)
            return {"backend": "openviking", "historical_only": True, "scope": scope,
                    "facts": facts, "runbooks": runbooks, "session_id": session_id,
                    "read_at": time.time(), "authority": "published-current-revisions"}
        except Exception as error:
            # Never turn an external memory outage into a cache hit or expose
            # provider credentials/error bodies through a model tool response.
            return {"backend": "openviking", "historical_only": True, "facts": [], "runbooks": [],
                    "error": "Knowledge unavailable: " + type(error).__name__, "read_at": time.time()}

    return lookup


def source_item(worker, session, item):
    database = (Path(worker) / "hermes/state.db").resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT content FROM messages WHERE session_id=? AND id=?", (session, item)).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise ValueError("Memory source is missing its original Session/Item bytes")
    return {"session_id": session, "item_id": item, "sha256": digest(row[0]), "evidence_path": str(database)}


def completed(root):
    for alias in ALIASES:
        worker = Path(root) / "patrols" / alias
        if not (worker / "patrol.sqlite").is_file():
            continue
        with closing(connect(worker, readonly=True)) as db:
            for service in SERVICES:
                for record in incidents(db, service):
                    if record["recovered_inspection_id"] is not None:
                        yield alias, worker, record


def observe_fact(worker, record):
    directory = worker / "pages" / record["incident_id"]
    page, branch = load_json(directory / "page.json"), load_json(directory / "investigation.json")
    verified_branch(worker, record, branch)
    if (page["incident_id"] != record["incident_id"] or page["session_id"] != branch["session_id"]
            or page["report_item_id"] != branch["report_item_id"] or page["report"] != branch["report"]):
        raise ValueError("Memory source page does not match its actual investigation")
    with closing(connect(worker, readonly=True)) as db:
        recovery = recovered_observation(db, record, page)
        recovered_record = dict(db.execute("SELECT * FROM inspections WHERE id=?", (record["recovered_inspection_id"],)).fetchone())
    proof = receipt(worker, record)
    proof["investigation"] = branch
    sources = [source_item(worker, record["session_id"], record["report_item_id"]),
               source_item(worker, branch["session_id"], branch["report_item_id"]),
               source_item(worker, recovered_record["session_id"], recovered_record["report_item_id"])]
    return proof, recovery, sources


def process_once(root, config, live=False):
    """Review independently of Skill learning, then drain the durable outbox."""
    from apps.oncall.service import JobHeartbeat
    root = Path(root).resolve()
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    store = KnowledgeStore(state / "knowledge.sqlite3")
    jobs = ControlStore(state / "jobs.sqlite3")
    summary = {"backend": "openviking", "namespace": config["namespace"], "status": "running",
               "reviewed": [], "updated_at": time.time()}
    for alias, worker, record in completed(root):
        observed = json.loads(record["observation"])
        service, env = observed["service"], observed["environment_id"]
        if config["instances"].get(alias) != env or service not in config["services"]:
            continue
        scope = {"tenant": config["tenant"], "environment": env, "service": service}
        try:
            proof, recovery, sources = observe_fact(worker, record)
        except Exception as error:
            summary.setdefault("source_errors", []).append({"incident_id": record["incident_id"],
                "instance": alias, "error_type": type(error).__name__})
            summary["status"] = "source_error"
            continue
        payload = {"incident_id": record["incident_id"], "scope": scope, "sources": sources}
        job = jobs.enqueue(scope=canonical(scope), key="review:" + record["incident_id"],
                           queue="memory", payload=payload, max_attempts=3)
        if job["status"] == "failed":
            summary.setdefault("failed_reviews", []).append({"incident_id": record["incident_id"],
                                                            "job_id": job["id"]})
            summary["status"] = "review_error"
            continue
        claimed = jobs.claim("memory-" + str(os.getpid()), queue="memory", task_id=job["id"], lease_seconds=30)
        if claimed is None:
            continue
        lease, current = claimed
        heartbeat = JobHeartbeat(jobs, lease)
        work = state / "reviews" / job["id"] / f"attempt-{current['attempts']}"
        work.mkdir(parents=True, exist_ok=False)
        try:
            heartbeat.fence()
            jobs.checkpoint(lease, {"stage": "reviewing", "sources": sources})
            proposal = review_facts(work, {alias: proof}, {alias: recovery}, state / "review-checkpoints.sqlite3", live)[alias]
            heartbeat.fence()
            if proposal["status"] == "proposed":
                key = "incident:" + record["incident_id"]
                previous = store.head(scope, key)
                if previous is not None and previous["content"] != proposal["evidence"]:
                    raise ValueError("An incident's verified historical record cannot be silently rewritten")
                if previous is None:
                    revision = store.revise(scope=scope, key=key, content=proposal["evidence"], sources=sources,
                        valid_until=recovery["window_end"] + config.get("fact_ttl_seconds", 2592000))
                else:
                    revision = previous
                jobs.checkpoint(lease, {"stage": "queued-for-publication", "revision_id": revision["revision_id"]})
                # A runbook revision is derived from an actual protected Skill
                # read. New method bytes replace the old head, not its evidence.
                skill = proof["skill"]
                method_key = "runbook:" + record["skill_name"]
                active_path = root / "learning/state.json"
                active = load_json(active_path)["active_sha256"] if active_path.is_file() else record["skill_sha256"]
                method = {"service": service, "skill": record["skill_name"],
                          "sha256": record["skill_sha256"], "body": skill["content"]}
                head = store.head(scope, method_key)
                if active == record["skill_sha256"] and (head is None or head["content"] != method
                        or head.get("valid_until") is not None and head["valid_until"] <= time.time()):
                    store.revise(scope=scope, key=method_key, content=method,
                        sources=[source_item(worker, record["session_id"], record["skill_item_id"])],
                        expected_revision=head["revision_id"] if head else None,
                        valid_until=time.time() + config.get("fact_ttl_seconds", 2592000))
            jobs.complete(lease, {"decision": proposal["status"], "incident_id": record["incident_id"]})
            summary["reviewed"].append({"incident_id": record["incident_id"], "decision": proposal["status"]})
        except Exception as error:
            jobs.fail(lease, type(error).__name__, retry_delay=2)
            summary.update(status="review_error", error_type=type(error).__name__)
        finally:
            heartbeat.close()
    deliveries = store.drain(provider(config), owner="memory-" + str(os.getpid()), limit=4,
                             lease_seconds=180, retry_delay=2)
    pending = store.pending()
    summary.update(pending_publications=pending, deliveries=deliveries, updated_at=time.time())
    try:
        summary["provider_available"] = provider(config, retrieval=True).health()
    except Exception:
        summary["provider_available"] = False
    if summary["status"] == "running":
        summary["status"] = ("unavailable" if not summary["provider_available"] else
                             "publication_pending" if pending else "ready")
    write_json(state / "status.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    config = load_binding(args.config)
    ensure_loopback_no_proxy()
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    with writer_lock(state, "worker-owner.sqlite", timeout=0):
        while True:
            try:
                process_once(args.root, config, args.live)
            except Exception as error:
                write_json(state / "status.json", {"backend": "openviking", "status": "error",
                    "error_type": type(error).__name__, "updated_at": time.time()})
            time.sleep(2)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
