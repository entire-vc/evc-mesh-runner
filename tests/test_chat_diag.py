import json
import tempfile
import unittest
from pathlib import Path

from fake_chat_mesh import FakeChatMesh
from mesh_runner import chat_diag
from mesh_runner.chat_delivery import DriverDelivery
from mesh_runner.chat_host import ChatRefused
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.chat_turn import ChatTurnRunner
from mesh_runner.host_admission import AdmissionClosed

SECRET = "sk-TESTSECRET-9f3a"
PROMPT = "PROMPT-TEXT-do-not-log"


def run_turn(plan_exc, tmp):
    mesh = FakeChatMesh()
    client = ChatClient("https://mesh.test", "secret", opener=mesh)
    mesh.queue_turn(PROMPT)
    turn = client.claim(["agent-1"], timeout=1)["turns"][0]

    def plan(_turn):
        raise plan_exc
    delivery = DriverDelivery("claude", plan, Path(tmp) / "chat" / "logs")
    out = ChatTurnRunner(client, delivery, sleep=lambda s: None, batch_seconds=0.02).run(turn)
    return mesh, turn, out


def lines(tmp):
    p = Path(tmp) / "chat" / "diagnostics.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()]


class ChatDiag(unittest.TestCase):
    def check(self, exc, reason, cls, code="delivery_failed"):
        with tempfile.TemporaryDirectory() as tmp:
            mesh, turn, out = run_turn(exc, tmp)
            entries = lines(tmp)
            self.assertEqual(len(entries), 1)               # once, not again from the produce stage
            e = entries[0]
            self.assertEqual((e["stage"], e["exc"], e["reason"]), ("plan", cls, reason))
            raw = (Path(tmp) / "chat" / "diagnostics.jsonl").read_text()
            for bad in (SECRET, PROMPT, tmp, "/Users", "Traceback"):
                self.assertNotIn(bad, raw)
            self.assertNotIn("/", e.get("file", ""))
            last = mesh.turns[turn["turn_id"]]["events"][-1]
            self.assertEqual((last["type"], last["code"]), ("error", code))   # terminal event unchanged
            return e

    def test_env_helper_denial(self):
        self.check(chat_diag.EnvDenied(f"env command failed (rc 1): {SECRET}", "env_command_failed"),
                   "env_command_failed", "EnvDenied")

    def test_pre_driver_exception(self):
        e = self.check(KeyError(SECRET), "unclassified", "KeyError")
        self.assertEqual((e["file"], e["func"]), ("chat_delivery.py", "run"))   # innermost own-package frame, basename only

    def test_barrier_refusal(self):
        self.check(AdmissionClosed(SECRET), "barrier_refused", "AdmissionClosed")

    def test_stable_refusal_code_kept(self):
        self.check(ChatRefused("headless_forbidden", SECRET), "chat_refused", "ChatRefused", "headless_forbidden")

    def test_reason_is_allowlisted(self):
        class Evil(Exception):
            diag_reason = SECRET
        self.assertIn(chat_diag.reason_of(Evil()), chat_diag.REASONS)

    def test_location_is_package_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                chat_diag.reason_of(None) and None
                from mesh_runner import runner  # noqa
                raise chat_diag.EnvDenied("x", "env_file_unreadable")
            except chat_diag.EnvDenied as exc:
                e = chat_diag.record("plan", exc, "abcdef123456", Path(tmp))
            self.assertEqual(e["reason"], "env_file_unreadable")
            self.assertEqual(e["turn"], "abcdef12")


if __name__ == "__main__":
    unittest.main()
