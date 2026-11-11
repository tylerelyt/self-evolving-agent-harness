"""Review recovered workbench incidents and recall them in fresh Hermes sessions."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "patrol"))

from environment import SERVICES, classify, load_json, write_json
from fact_handoff import connection as recall_connection
from fact_handoff import read_evidence as verify_recall
from fleet import receipt, review_facts
from implementation import load_implementation
from investigation import followup_receipts
from patrol import connect, decode_report, incidents, writer_lock
from shared_facts import FACT_FIELDS, read_fact, validate_fact


ALIASES = ("instance-a", "instance-b")


def matches_publication(state, entry):
    evidence = {key: entry[key] for key in FACT_FIELDS}
    validate_fact(evidence)
    scope = tuple(evidence[key] for key in ("incident_id", "service", "environment_id"))
    return read_fact(Path(state["shared_store"]).resolve(), scope) == evidence


def memory_state(root):
    root = Path(root)
    path = root / "memory" / "state.json"
    if path.is_file():
        state = load_json(path)
    else:
        state = {"status": "empty", "facts": [], "recalls": [],
                 "shared_store": str(root / "memory" / "shared-facts.sqlite3")}
    reviews = state.setdefault("reviews", [])
    # Older workbenches put a successful no-change decision in the fact index.
    # Retain that review for idempotence without presenting its evidence as Memory.
    published, unavailable = [], []
    for entry in state["facts"]:
        if entry.get("review_status") == "proposed":
            try:
                if matches_publication(state, entry):
                    published.append(entry)
                    continue
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                pass
            unavailable.append(entry.get("incident_id", "unknown"))
        if entry not in reviews:
            reviews.append(entry)
    state["facts"] = published
    if unavailable:
        state["last_error"] = "Published Memory fact is unavailable or changed: " + ", ".join(unavailable)
    if state["status"] != "error":
        state["status"] = "ready" if state["facts"] else "empty"
    return state


def save_state(root, state):
    state["updated_at"] = time.time()
    write_json(root / "memory" / "state.json", state)


def verified_branch(worker, record, branch):
    observation = json.loads(record["observation"])
    scope = {"incident_id": record["incident_id"], "service": observation["service"],
             "environment_id": observation["environment_id"]}
    if (any(branch.get(key) != value for key, value in scope.items())
            or branch["source_session_id"] != record["session_id"]
            or branch["source_report_item_id"] != record["report_item_id"]
            or branch["skill_sha256"] != record["skill_sha256"]
            or branch["session_id"] == record["session_id"]):
        raise ValueError("Memory source does not match the original incident and report")
    database = worker / "hermes" / "state.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        parent = db.execute("SELECT parent_session_id FROM sessions WHERE id = ?",
                            (branch["session_id"],)).fetchone()
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM messages WHERE session_id = ? AND id > ? AND id <= ? ORDER BY id",
            (branch["session_id"], branch["copied_report_item_id"], branch["report_item_id"]),
        )]
    for row in rows:
        row["tool_calls"] = json.loads(row["tool_calls"] or "[]")
    if parent is None or parent[0] != record["session_id"] or not rows:
        raise ValueError("Memory source is missing its persisted investigation branch")
    skill_item, probes = followup_receipts(rows, scope, record["skill_sha256"])
    if (skill_item != branch["skill_item_id"] or probes != branch["probes"]
            or rows[-1]["id"] != branch["report_item_id"]
            or decode_report(rows[-1]["content"]) != branch["report"]):
        raise ValueError("Memory proposal differs from the persisted investigation evidence")
    return scope


def recovered_observation(db, record, page):
    recovery = db.execute("SELECT * FROM inspections WHERE id = ?",
                          (record["recovered_inspection_id"],)).fetchone()
    if recovery is None:
        raise ValueError("Recovered incident does not reference a stored checkpoint")
    current = json.loads(recovery["observation"])
    original = json.loads(record["observation"])
    if (recovery["service"] != original["service"] or recovery["status"] != "healthy"
            or current["environment_id"] != original["environment_id"]
            or current["window_start"] <= page["paged_at"]
            or classify(current) != "healthy" or current["error_rate"] != 0):
        raise ValueError("Recovery checkpoint is outside the paged incident's scope")
    earlier = db.execute(
        """SELECT observation FROM inspections WHERE service = ? AND environment = ? AND region = ?
           AND status = 'healthy' AND checked_at < ? ORDER BY checked_at DESC""",
        (recovery["service"], recovery["environment"], recovery["region"], current["window_start"]),
    )
    for row in earlier:
        previous = json.loads(row["observation"])
        if (previous["environment_id"] == current["environment_id"]
                and previous["window_start"] > page["paged_at"]
                and previous["window_end"] < current["window_start"]
                and classify(previous) == "healthy" and previous["error_rate"] == 0):
            return current
    raise ValueError("Memory review requires two recorded non-overlapping healthy windows")


def review(workbench_root, run_root, environments, live=False, incident_id=None):
    """Publish facts only after checking the original SessionDB and recovery windows."""
    root, run = Path(workbench_root).resolve(), Path(run_root).resolve()
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    with writer_lock(memory, timeout=1):
        state = memory_state(root)
        # Completed keep decisions are reusable without publication. A proposal
        # is reusable only while its exact fact remains in the shared store.
        prior = {entry["incident_id"]: entry for entry in state["facts"] + [
            item for item in state["reviews"] if item.get("review_status") == "keep"
        ]}
        reviewed, published, kept = [], [], []
        for alias in ALIASES:
            worker = run / "patrols" / alias
            if not (worker / "patrol.sqlite").is_file():
                continue
            with closing(connect(worker, readonly=True)) as db:
                for service in SERVICES:
                    for record in incidents(db, service):
                        if incident_id is not None and record["incident_id"] != incident_id:
                            continue
                        record_id = record["incident_id"]
                        if record["recovered_inspection_id"] is None:
                            continue
                        if record_id in prior:
                            saved = prior[record_id]
                            if (saved["source_session_id"] != record["session_id"]
                                    or saved["source_report_item_id"] != record["report_item_id"]):
                                raise ValueError("Historical incident already has different source evidence")
                            continue
                        inbox = worker / "pages" / record_id
                        page = load_json(inbox / "page.json")
                        branch = load_json(inbox / "investigation.json")
                        verified_branch(worker, record, branch)
                        if (page["incident_id"] != record_id or page["session_id"] != branch["session_id"]
                                or page["report_item_id"] != branch["report_item_id"]
                                or page["report"] != branch["report"]):
                            raise ValueError("Memory source page does not refer to the verified branch")
                        recovered = recovered_observation(db, record, page)
                        proof = receipt(worker, record)
                        skill = proof["skill"]
                        if hashlib.sha256(skill["content"].encode()).hexdigest() != record["skill_sha256"]:
                            raise ValueError("Original Skill content differs from its recorded hash")
                        proof["investigation"] = branch
                        attempt = memory / "reviews" / record_id / uuid4().hex
                        attempt.mkdir(parents=True)
                        entry = review_facts(attempt, {alias: proof}, {alias: recovered},
                                             Path(state["shared_store"]), live)[alias]
                        if entry["status"] not in ("proposed", "keep"):
                            raise ValueError("Memory review did not return a completed decision")
                        fact = {**entry["evidence"], "source_run": str(run),
                                "review_status": entry["status"], "review_path": str(attempt)}
                        if entry["status"] == "proposed":
                            if not matches_publication(state, fact):
                                raise ValueError("Memory proposal was not published with the verified evidence")
                            state["facts"].append(fact)
                            published.append(record_id)
                        else:
                            kept.append(record_id)
                        state["reviews"].append(fact)
                        prior[record_id] = fact
                        reviewed.append(record_id)
                        state.update(status="ready" if state["facts"] else "empty", last_error=None)
                        save_state(root, state)
        state["status"] = "ready" if state["facts"] else "empty"
        save_state(root, state)
        return {"status": state["status"], "reviewed": reviewed,
                "published": published, "kept": kept,
                "facts": len(state["facts"]), "state": str(memory / "state.json")}


def review_runs(workbench_root, environments, live=False, incident_id=None):
    """Review every persisted Agent run, including runs replaced by new implementations."""
    root = Path(workbench_root).resolve()
    runs = sorted(path.parent for path in (root / "runs").glob("*/service.json") if path.is_file())
    reviewed, published, kept = [], [], []
    for run in runs:
        result = review(root, run, environments, live, incident_id)
        reviewed.extend(result["reviewed"])
        published.extend(result["published"])
        kept.extend(result["kept"])
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    with writer_lock(memory, timeout=1):
        state = memory_state(root)
        state.update(status="ready" if state["facts"] else "empty", last_error=None)
        save_state(root, state)
        return {"status": state["status"], "reviewed": reviewed,
                "published": published, "kept": kept,
                "facts": len(state["facts"]), "state": str(memory / "state.json"),
                "runs": [str(run) for run in runs]}


def recall(workbench_root, environments, implementation_file, incident_id, live=False):
    """Run the selected Agent's memory hook in a new native Hermes session."""
    root = Path(workbench_root).resolve()
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    with writer_lock(memory, timeout=1):
        state = memory_state(root)
        fact = next((entry for entry in state["facts"]
                     if entry["incident_id"] == incident_id and entry["review_status"] == "proposed"), None)
        if fact is None:
            raise ValueError("Select an incident with a verified and published Memory fact")
        implementation = load_implementation(implementation_file)
        identifier = uuid4().hex
        session_state = memory / "recalls" / identifier
        session_state.mkdir(parents=True)
        evidence = session_state / "handoff.json"
        scope = {key: fact[key] for key in ("incident_id", "service", "environment_id")}
        with recall_connection(not live) as url:
            options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if live else "local-fixture",
                       "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if live else "handoff-fixture"}
            result = implementation.recall_memory(
                session_state, Path(state["shared_store"]), Path(environments[fact["instance"]]), scope, options,
            )
        database = session_state / "hermes" / "state.db"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                                                   (result["session_id"],))]
        for row in rows:
            row["tool_calls"] = json.loads(row["tool_calls"] or "[]")
        if not rows:
            raise ValueError("Memory recall did not leave a persisted Hermes session")
        verified = verify_recall(rows, rows[0]["content"], scope, result["session_id"])
        if result != verified:
            raise ValueError("Memory recall result differs from its persisted requests and tool results")
        if (result["status"] != "recalled" or result["fact"] is None
                or result["scope"] != scope
                or result["session_id"] in (fact["source_session_id"], fact["investigation_session_id"])):
            raise ValueError("Memory hook did not return a new session with the verified source citation")
        expected_fact = {key: fact[key] for key in FACT_FIELDS}
        validate_fact(expected_fact)
        validate_fact(result["fact"])
        if result["fact"] != expected_fact:
            raise ValueError("Memory recall fact differs from the verified historical evidence")
        write_json(evidence, result)
        entry = {"id": identifier, "incident_id": incident_id, "status": result["status"],
                 "session_id": result["session_id"], "report_item_id": result["report_item_id"],
                 "state": str(session_state), "evidence": str(evidence),
                 "source_run": fact["source_run"], "created_at": time.time()}
        state["recalls"].append(entry)
        state.update(status="ready", last_error=None)
        save_state(root, state)
        return {**entry, "report": result["report"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--run", type=Path, help="Review this run; omit to review all persisted runs")
    parser.add_argument("--environment-a", type=Path, required=True)
    parser.add_argument("--environment-b", type=Path, required=True)
    parser.add_argument("--action", choices=("review", "recall"), required=True)
    parser.add_argument("--implementation", type=Path, required=True)
    parser.add_argument("--incident-id")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    environments = {"instance-a": args.environment_a.resolve(), "instance-b": args.environment_b.resolve()}
    try:
        if args.action == "review":
            if args.run is None:
                result = review_runs(args.state, environments, args.live, args.incident_id)
            else:
                result = review(args.state, args.run, environments, args.live, args.incident_id)
        else:
            if not args.incident_id:
                parser.error("Memory recall requires --incident-id")
            result = recall(args.state, environments, args.implementation, args.incident_id, args.live)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        memory = args.state.resolve() / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        try:
            with writer_lock(memory, timeout=1):
                state = memory_state(args.state.resolve())
                state.update(status="error", last_error=str(exc))
                save_state(args.state.resolve(), state)
        except RuntimeError:
            pass
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
