"""The read-only no_outcome classifier sorts a Claude turn by what its transcript shows ."""
import datetime as dt
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mesh_runner import no_outcome_classes as noc

AT = "2026-11-14T12:00:00Z"


def ts(sec: int) -> str:
    """``sec`` seconds after 11:50:00, ten minutes before the event."""
    return (dt.datetime(2026, 11, 14, 11, 50, tzinfo=dt.timezone.utc)
            + dt.timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prompt(sec, text="[runner] Task #00000001"):
    return {"type": "user", "timestamp": ts(sec), "message": {"role": "user", "content": text}}


def say(sec, text):
    return {"type": "assistant", "timestamp": ts(sec), "message": {"content": [{"type": "text", "text": text}]}}


def use(sec, uid, name, **inp):
    return {"type": "assistant", "timestamp": ts(sec),
            "message": {"content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]}}


def result(sec, uid, body, error=False):
    return {"type": "user", "timestamp": ts(sec), "message": {"content": [
        {"type": "tool_result", "tool_use_id": uid, "content": body, "is_error": error}]}}


def notification(sec, task):
    return {"type": "user", "timestamp": ts(sec), "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "<task-notification>\n<task-id>%s</task-id>\n"
                        "<status>completed</status>\n</task-notification>" % task}}


def background_bash(sec, task="bfx01"):
    return [use(sec, "b-" + task, "Bash", command="pytest", run_in_background=True),
            result(sec + 1, "b-" + task, "Command running in background with ID: %s. Output is being "
                   "written to: /tmp/x" % task)]


class ClassifyTurn(unittest.TestCase):
    def classify(self, records):
        return noc.classify_turn(records, noc.timestamp(AT))

    def test_text_while_its_background_bash_runs(self):
        kind, facts = self.classify([prompt(0), *background_bash(10), say(20, "Tests run in the background")])
        self.assertEqual(kind, "text_background")
        self.assertEqual(facts["background"], ["Bash:bfx01"])
        self.assertEqual(facts["background_age_s"], 9)
        self.assertEqual(facts["last_text"], "Tests run in the background")

    def test_monitor_agent_and_a_moved_to_background_command_count_too(self):
        records = [prompt(0),
                   use(5, "m1", "Monitor", command="tail -f x"),
                   result(6, "m1", "Monitor started (task bmon1, expires in 30m unless the source ends first)"),
                   use(7, "a1", "Agent", run_in_background=True),
                   result(8, "a1", "Async agent launched successfully.\nagentId: a2ac68cd7b9f3c87d (internal)"),
                   use(9, "s1", "Bash", command="make test"),
                   result(10, "s1", "Command did not complete within its 600s timeout and was moved to the "
                          "background (ID: bslow1). Output is being written to: /tmp/y"),
                   say(20, "waiting")]
        kind, facts = self.classify(records)
        self.assertEqual(kind, "text_background")
        self.assertEqual(facts["background"], ["Agent:a2ac68cd7b9f3c87d", "Bash:bslow1", "Monitor:bmon1"])

    def test_a_background_task_reported_finished_before_the_text_is_not_open(self):
        records = [prompt(0), *background_bash(10), notification(15, "bfx01"), say(20, "done reading")]
        self.assertEqual(self.classify(records)[0], "text_no_command")
        stopped = [prompt(0), *background_bash(10), use(14, "k", "TaskStop", task_id="bfx01"), say(20, "stopped")]
        self.assertEqual(self.classify(stopped)[0], "text_no_command")

    def test_a_notification_after_the_turn_ended_leaves_the_task_open_at_its_end(self):
        """As seen live: the notification came 3 s after the Stop."""
        records = [prompt(0), *background_bash(10), say(20, "I'll continue once the monitor reports"),
                   notification(23, "bfx01")]
        self.assertEqual(self.classify(records)[0], "text_background")

    def test_a_turn_command_in_the_turn(self):
        ok = [prompt(0), use(10, "w", "mcp__evc-mesh__wait_for", on="card"), result(11, "w", "ok"), say(12, "x")]
        self.assertEqual(self.classify(ok), ("command_ok", {"turn_started_at": "2026-11-14T11:50:00+00:00",
                                                            "last_text": "x", "command": "wait_for"}))
        bad = [prompt(0), use(10, "w", "mcp__evc-mesh__finish_task"),
               result(11, "w", "finish_task refused: 400", error=True), say(12, "refused")]
        self.assertEqual(self.classify(bad)[0], "command_rejected")

    def test_a_command_of_an_earlier_turn_does_not_count_for_this_one(self):
        records = [prompt(0, "first"), use(5, "w", "mcp__evc-mesh__continue_next"), result(6, "w", "ok"),
                   prompt(30, "second"), notification(31, "zzz"), say(40, "plain text")]
        self.assertEqual(self.classify(records)[0], "text_no_command")

    def test_no_assistant_record_in_the_last_15_minutes(self):
        records = [{"type": "user", "timestamp": "2026-11-14T11:00:00Z", "message": {"content": "p"}},
                   {"type": "assistant", "timestamp": "2026-11-14T11:40:00Z",
                    "message": {"content": [{"type": "text", "text": "old"}]}}]
        self.assertEqual(self.classify(records)[0], "no_activity")

    def test_records_after_the_event_are_not_read(self):
        records = [prompt(0), say(10, "text"), use(630, "w", "mcp__evc-mesh__wait_for")]   # 12:00:30
        self.assertEqual(self.classify(records)[0], "text_no_command")


class Report(unittest.TestCase):
    def test_cli_reads_the_database_read_only_and_counts_by_class_and_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects" / "-lane"
            projects.mkdir(parents=True)
            sid = "11111111-2222-3333-4444-555555555555"
            with open(projects / (sid + ".jsonl"), "w") as fh:
                for rec in [prompt(0), *background_bash(10), say(20, "waiting for the tests")]:
                    fh.write(json.dumps(rec) + "\n")
            db = root / "runner.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE attempts (id TEXT, agent TEXT, harness TEXT, session_id TEXT);"
                "CREATE TABLE events (at TEXT, attempt TEXT, kind TEXT, detail TEXT);")
            conn.executemany("INSERT INTO attempts VALUES (?,?,?,?)", [
                ("a1", "builder", "claude", sid), ("a2", "reviewer", "claude", "missing-session"),
                ("a3", "Gpt", "codex", "t")])
            conn.executemany("INSERT INTO events VALUES (?,?,?,?)", [
                ("2026-11-14T12:00:00.100+00:00", "a1", "no_outcome", ""),
                ("2026-11-14T12:00:05.000+00:00", "a2", "no_outcome", ""),
                ("2026-11-14T12:00:06.000+00:00", "a3", "no_outcome", ""),        # not a Claude lane
                ("2026-11-14T12:00:07.000+00:00", "a1", "readable_outcome", "next"),
                ("2026-11-15T12:00:07.000+00:00", "a1", "no_outcome", "")])     # outside the window
            conn.commit()
            conn.close()
            before = db.read_bytes()
            out = io.StringIO()
            with redirect_stdout(out):
                rc = noc.main(["--db", str(db), "--since", "2026-11-14T00:00:00Z", "--until",
                               "2026-11-15T00:00:00Z", "--projects", str(root / "projects"), "--examples", "1"])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["total"], 2)
            self.assertEqual(data["classes"]["text_background"], 1)
            self.assertEqual(data["classes"]["no_transcript"], 1)
            self.assertEqual(data["lanes"], {"builder": {"text_background": 1}, "reviewer": {"no_transcript": 1}})
            self.assertEqual(data["examples"]["text_background"][0]["background"], ["Bash:bfx01"])
            self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
