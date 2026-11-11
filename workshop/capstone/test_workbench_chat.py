"""Conversation boundaries remain stable across versions and interrupted jobs."""

from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import workbench_chat as chat


class SessionStore:
    """Small SQLite adapter at the native-turn boundary; never calls a model."""

    def __init__(self, state):
        path = state / "hermes" / "state.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, model TEXT);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                tool_calls TEXT, tool_name TEXT, timestamp REAL
            );
        """)

    def get_session(self, identifier):
        return self.db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()

    def create_session(self, identifier, _source, *, model, system_prompt):
        self.db.execute("INSERT INTO sessions VALUES (?,?)", (identifier, model))
        self.db.commit()

    def append(self, identifier, role, content, *, tool_name=None, tool_calls=None):
        with self.db:
            self.db.execute("INSERT INTO messages(session_id,role,content,tool_calls,tool_name,timestamp) "
                            "VALUES (?,?,?,?,?,?)", (identifier, role, content, json.dumps(tool_calls or []),
                                                     tool_name, time.time()))
        return dict(self.db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1").fetchone())

    def history(self, identifier):
        return [dict(row) for row in self.db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id",
                                                    (identifier,))]


class ConversationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "chat" / ("a" * 32)
        self.state.mkdir(parents=True)
        self.turns = []

    def write_version(self, name="run-one", *, stage=1, source_sha="a" * 64, content="Approved initial method"):
        run = self.root / "runs" / name
        digest = hashlib.sha256(content.encode()).hexdigest()
        for path in (run / "receivers/instance-a" / chat.ONCALL_SKILL / "SKILL.md",
                     run / "formal/versions" / digest / chat.ONCALL_SKILL / "SKILL.md"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        chat.write_json(self.root / "workbench.json", {"active_run": name, "active_stage": stage,
                                                        "running_sha": source_sha})
        return digest

    @contextmanager
    def session_store(self, state):
        sessions = SessionStore(state)
        try:
            yield sessions
        finally:
            sessions.db.close()

    def native_turn(self, sessions, identifier, prompt, tools, options):
        self.turns.append({"session": identifier, "tools": set(tools), "options": dict(options),
                           "prior": sessions.history(identifier),
                           "book": tools["oncall_runbook"]["handler"]()})
        user = sessions.append(identifier, "user", prompt)
        answer = sessions.append(identifier, "assistant", json.dumps("This is a control test answer."))
        return [user, answer]

    def ask(self, question="Check the current service", *, context=None):
        with (patch.object(chat, "connection", return_value=nullcontext("http://model.invalid")),
              patch.object(chat, "open_sessions", side_effect=self.session_store),
              patch.object(chat, "native_turn", side_effect=self.native_turn)):
            return chat.ask(self.state, self.root, question, context, offline=True)

    def test_stage_zero_and_invalid_stages_never_reach_a_model(self):
        for stage in (0, -1, 5, True, "4"):
            with self.subTest(stage=stage):
                self.write_version(stage=stage)
                metadata = self.state / "conversation.json"
                metadata.unlink(missing_ok=True)
                with patch.object(chat, "connection") as connection:
                    with self.assertRaisesRegex(ValueError, "offline"):
                        chat.ask(self.state, self.root, "Check the service", offline=True)
                    connection.assert_not_called()
                self.assertFalse((self.state / "hermes/state.db").exists())
                self.assertEqual(chat.simple_tools(self.root, {}, stage), {})

    def test_later_capabilities_are_not_exposed_before_their_stage(self):
        basic = {"service_metrics", "request_logs", "recent_changes", "incident_history", "oncall_runbook"}
        for stage, additions in ((1, set()), (2, {"investigate_instances"}),
                                 (3, {"investigate_instances", "memory_lookup"}),
                                 (4, {"investigate_instances", "memory_lookup", "skill_history"})):
            with self.subTest(stage=stage):
                self.state = self.root / "chat" / str(stage)
                self.state.mkdir()
                self.write_version(stage=stage)
                self.ask()
                self.assertEqual(self.turns[-1]["tools"], basic | additions)

    def test_old_conversation_keeps_its_version_and_history_after_code_application(self):
        digest = self.write_version(stage=1, source_sha="a" * 64, content="First approved Skill")
        first = self.ask("Remember the incident name Pine")
        self.write_version("run-two", stage=4, source_sha="b" * 64, content="New approved Skill")
        second = self.ask("What name did I ask you to remember?", context={"page": "logs"})
        saved = chat.load_json(self.state / "conversation.json")

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(saved["active_stage"], 1)
        self.assertEqual(saved["active_run"], "run-one")
        self.assertEqual(saved["implementation_sha256"], "a" * 64)
        self.assertEqual(saved["skill_sha256"], digest)
        self.assertEqual(self.turns[0]["book"], self.turns[1]["book"])
        self.assertNotIn("investigate_instances", self.turns[1]["tools"])
        self.assertNotIn("memory_lookup", self.turns[1]["tools"])
        self.assertTrue(any("Pine" in row["content"] for row in self.turns[1]["prior"]))
        self.assertEqual(len(saved["turns"]), 2)

        self.state = self.root / "chat" / ("b" * 32)
        self.state.mkdir()
        self.ask()
        self.assertIn("investigate_instances", self.turns[-1]["tools"])
        self.assertIn("skill_history", self.turns[-1]["tools"])
        self.assertNotEqual(first["session_id"], self.turns[-1]["session"])

    def test_existing_model_and_mode_are_not_silently_changed(self):
        self.write_version()
        chat.write_json(self.state / "conversation.json", {"id": self.state.name,
                        "mode": "local-script-model", "model": "pinned-model", "active_stage": 1})
        self.ask()
        with patch.dict(os.environ, {"GLM_MODEL": "new-process-model"}):
            self.ask("Continue this conversation")
        self.assertEqual(self.turns[-1]["options"]["model"], "pinned-model")
        before = (self.state / "conversation.json").read_bytes()
        with patch.object(chat, "connection") as connection, patch.object(chat, "native_turn") as native:
            with self.assertRaisesRegex(ValueError, "new conversation"):
                chat.ask(self.state, self.root, "Use the configured model", offline=False)
            connection.assert_not_called()
            native.assert_not_called()
        self.assertEqual((self.state / "conversation.json").read_bytes(), before)

    def test_tampered_pinned_skill_never_reaches_the_model(self):
        self.write_version()
        self.ask()
        metadata = chat.load_json(self.state / "conversation.json")
        metadata["runbook"]["content"] += " tampered"
        chat.write_json(self.state / "conversation.json", metadata)
        with patch.object(chat, "connection") as connection:
            with self.assertRaisesRegex(ValueError, "recorded hash"):
                chat.ask(self.state, self.root, "Continue", offline=True)
            connection.assert_not_called()

    def test_snapshot_displays_user_text_without_changing_original_session_records(self):
        self.write_version()
        first = self.ask("Look at the payments service", context={"page": "metrics", "service": "payments"})
        self.ask("Is it still the same incident?", context={"page": "logs", "service": "payments"})
        database = self.state / "hermes/state.db"
        with sqlite3.connect(database) as connection:
            stored = connection.execute("SELECT content FROM messages WHERE role='user' ORDER BY id").fetchall()
        result = chat.snapshot(self.state)
        users = [row for row in result["messages"] if row["role"] == "user"]
        self.assertEqual(result["conversation"]["session_id"], first["session_id"])
        self.assertEqual([row["content"] for row in users], ["Look at the payments service", "Is it still the same incident?"])
        self.assertTrue(all(row[0].startswith(chat.PREFIX) for row in stored))
        self.assertTrue(all(isinstance(row["tool_calls"], list) for row in result["messages"]))
        self.assertTrue(all("created_at" in row for row in result["messages"]))
        with sqlite3.connect(database) as connection:
            self.assertEqual(stored, connection.execute("SELECT content FROM messages WHERE role='user' ORDER BY id").fetchall())


class InvestigationCleanupTests(unittest.TestCase):
    def test_cancellation_finishes_all_thread_cards_and_leaves_no_child_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "chat" / "conversation"
            state.mkdir(parents=True)
            chat.write_json(state / "conversation.json", {"id": "conversation", "active_stage": 4})
            children = []
            cancelled = False
            real_popen = subprocess.Popen
            read_json = chat.optional_json

            def child_process(_command, **options):
                code = "pass" if not children else "import time; time.sleep(60)"
                process = real_popen([sys.executable, "-c", code], **options)
                if not children:
                    process.wait(timeout=5)
                children.append(process)
                return process

            def cancel_while_reading_finished_child(path, default=None):
                nonlocal cancelled
                if path.name == "last-result.json" and not cancelled:
                    cancelled = True
                    raise KeyboardInterrupt()
                return read_json(path, default)

            try:
                with patch.object(chat.subprocess, "Popen", side_effect=child_process), \
                     patch.object(chat, "optional_json", side_effect=cancel_while_reading_finished_child):
                    with self.assertRaises(KeyboardInterrupt):
                        chat.investigate(root, state, list(chat.ALIASES), "Inspect both environments", {}, True)
                self.assertEqual(len(children), 2)
                self.assertTrue(all(process.poll() is not None for process in children))
                rows = chat.load_json(state / "threads.json")
                self.assertEqual(len(rows), 2)
                self.assertFalse(any(row["status"] == "running" for row in rows))
                self.assertTrue(all(row.get("finished_at") for row in rows))
            finally:
                for process in children:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)


class PublishedMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.store = self.memory / "shared-facts.sqlite3"
        self.fact = {"incident_id": "lab-payments-0123456789ab", "instance": "instance-a",
                     "environment_id": "a" * 32, "service": "payments",
                     "source_session_id": "patrol-payments-" + "b" * 32,
                     "source_report_item_id": 10,
                     "investigation_session_id": "patrol-fork-" + "c" * 32,
                     "investigation_report_item_id": 30, "probe_statuses": [503, 503, 503],
                     "recovery_end": 130.0, "source_skill_sha256": "d" * 64}
        from shared_facts import publish_fact
        publish_fact(self.store, self.fact)

    def save_review(self, entry):
        chat.write_json(self.memory / "state.json", {"facts": [entry], "recalls": []})

    def test_review_evidence_is_not_memory_until_the_complete_fact_is_published(self):
        candidates = {
            "keep_even_when_same_incident_is_published": {**self.fact, "review_status": "keep"},
            "proposed_but_not_published": {**self.fact, "review_status": "proposed",
                                           "incident_id": "lab-payments-000000000002"},
            "proposed_with_wrong_source_item": {**self.fact, "review_status": "proposed",
                                                 "source_report_item_id": 1000010},
        }
        unchanged = self.store.read_bytes()
        for name, entry in candidates.items():
            with self.subTest(case=name):
                self.save_review(entry)
                result = chat.memories(self.root, {})
                self.assertEqual(result["facts"], [])
                self.assertTrue(result["historical_only"])
                self.assertTrue(result["published_only"])
                self.assertNotIn("publication_status", result)
                self.assertTrue(result["current_state_requires_fresh_metrics"])
                self.assertEqual(self.store.read_bytes(), unchanged)

    def test_published_fact_is_returned_only_with_its_exact_review_and_scope(self):
        from shared_facts import FACT_FIELDS

        entry = {**self.fact, "review_status": "proposed", "source_run": "recorded-run",
                 "review_path": "/private/internal-review", "internal_note": "review implementation detail"}
        self.save_review(entry)
        unchanged = self.store.read_bytes()
        result = chat.memories(self.root, {"instance": "instance-a", "service": "payments",
                                            "incident_id": self.fact["incident_id"]})
        self.assertEqual(len(result["facts"]), 1)
        visible = result["facts"][0]
        self.assertEqual(set(visible), FACT_FIELDS | {"publication_status", "recovery_end_iso"})
        self.assertEqual({key: visible[key] for key in FACT_FIELDS}, self.fact)
        self.assertEqual(visible["publication_status"], "published")
        self.assertEqual(visible["recovery_end_iso"], "1970-01-01T08:02:10.000+08:00")
        self.assertEqual(result["sources"], [{"incident_id": self.fact["incident_id"], "source_run": "recorded-run"}])
        self.assertTrue(result["published_only"])
        self.assertEqual(result["time_zone"], "Asia/Shanghai (+08:00)")
        for internal in ("review_status", "review_path", "internal_note", "/private/internal-review"):
            self.assertNotIn(internal, json.dumps(result))
        for context in ({"instance": "instance-b"}, {"service": "orders"},
                        {"incident_id": "lab-payments-000000000003"}):
            with self.subTest(context=context):
                self.assertEqual(chat.memories(self.root, context)["facts"], [])
        self.assertEqual(self.store.read_bytes(), unchanged)


class TimestampDisplayTests(unittest.TestCase):
    def test_display_timestamps_have_explicit_local_offsets_and_preserve_unix_receipts(self):
        original = {"time": 0, "nested": [{"window_start": 0, "checked_at": True}], "count": 3}
        saved = json.dumps(original)
        result = chat.annotate_times(original)
        self.assertEqual(result["time"], 0)
        self.assertEqual(result["time_iso"], "1970-01-01T08:00:00.000+08:00")
        self.assertEqual(result["nested"][0]["window_start"], 0)
        self.assertEqual(result["nested"][0]["window_start_iso"], "1970-01-01T08:00:00.000+08:00")
        self.assertNotIn("checked_at_iso", result["nested"][0])
        self.assertNotIn("count_iso", result)
        self.assertEqual(json.dumps(original), saved)


if __name__ == "__main__":
    unittest.main()
