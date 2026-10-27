import threading
import time
import unittest

from fake_chat_mesh import FakeChatMesh
from mesh_runner.chat_protocol import ChatClient, ChatError
from mesh_runner.chat_turn import ChatTurnRunner


class Scripted:
    """Delivery that yields a fixed script; `hold` pauses it until released or cancelled."""

    def __init__(self, events, hold=None):
        self.events, self.hold, self.seen_cancel = events, hold, False
        self.delivered = []

    def run(self, turn, cancelled):
        self.delivered.append(turn["message"]["body"])
        for ev in self.events:
            if ev == "HOLD":
                for _ in range(200):
                    if cancelled():
                        self.seen_cancel = True
                        yield {"type": "final", "stop_reason": "cancelled"}
                        return
                    time.sleep(0.01)
                continue
            yield ev


def make(delivery, **kw):
    mesh = FakeChatMesh()
    client = ChatClient("https://mesh.test", "secret", opener=mesh)
    runner = ChatTurnRunner(client, delivery, sleep=lambda s: None, batch_seconds=0.02, **kw)
    return mesh, client, runner


def claim(mesh, client, text="hi"):
    mesh.queue_turn(text)
    return client.claim(["agent-1"], timeout=1)["turns"][0]


class ChatTurn(unittest.TestCase):
    def test_streams_in_order_and_finishes(self):
        d = Scripted([{"type": "session", "session_id": "s1", "harness": "claude", "profile": "p"},
                      {"type": "text_delta", "text": "Hel"}, {"type": "text_delta", "text": "lo"},
                      {"type": "usage", "tokens_in": 5, "tokens_out": 2},
                      {"type": "final", "stop_reason": "end_turn"}])
        mesh, client, runner = make(d)
        turn = claim(mesh, client, "ping")
        out = runner.run(turn)
        t = mesh.turns[turn["turn_id"]]
        self.assertEqual(out.state, "done")
        self.assertEqual(t["text"], "Hello")
        self.assertEqual([e["seq"] for e in t["events"]], [1, 2, 3, 4, 5])
        self.assertEqual(d.delivered, ["ping"])
        # start came before any events: the message reaches the model only after start
        kinds = [p for _, p in mesh.requests if "/turns/" in p]
        self.assertTrue(kinds[0].endswith("/start"))

    def test_first_event_logs_where_the_turn_seconds_went_once(self):
        d = Scripted([{"type": "text_delta", "text": "a"}, {"type": "text_delta", "text": "b"},
                      {"type": "final", "stop_reason": "end_turn"}])
        mesh, client, runner = make(d)
        turn = claim(mesh, client)
        with self.assertLogs("mesh_runner", level="INFO") as cm:
            runner.run(turn)
        lines = [m for m in cm.output if "timing: start_call=" in m and "first_event=" in m]
        self.assertEqual(len(lines), 1)

    def test_a_delivery_that_raises_before_any_event_still_logs_its_timing(self):
        class Boom:
            def run(self, turn, cancelled):
                raise RuntimeError("tmux gone")
                yield
        mesh, client, runner = make(Boom())
        turn = claim(mesh, client)
        with self.assertLogs("mesh_runner", level="INFO") as cm:
            runner.run(turn)
        self.assertEqual(len([m for m in cm.output if "timing: start_call=" in m]), 1)

    def test_a_turn_cancelled_at_start_still_logs_its_timing(self):
        d = Scripted([{"type": "final", "stop_reason": "end_turn"}])
        mesh, client, runner = make(d)
        turn = claim(mesh, client)
        mesh.turns[turn["turn_id"]]["cancel_requested"] = True
        with self.assertLogs("mesh_runner", level="INFO") as cm:
            runner.run(turn)
        self.assertEqual(len([m for m in cm.output if "timing: start_call=" in m]), 1)

    def test_a_delivery_that_yields_nothing_still_logs_its_timing(self):
        mesh, client, runner = make(Scripted([]))
        turn = claim(mesh, client)
        with self.assertLogs("mesh_runner", level="INFO") as cm:
            runner.run(turn)
        self.assertEqual(len([m for m in cm.output if "timing: start_call=" in m]), 1)

    def test_stale_claim_on_start_drops_without_touching_the_model(self):
        d = Scripted([{"type": "final", "stop_reason": "end_turn"}])
        mesh, client, runner = make(d)
        turn = claim(mesh, client)
        turn["claim_token"] = "old"
        out = runner.run(turn)
        self.assertEqual(out.state, "released")
        self.assertEqual(d.delivered, [])

    def test_stop_interrupts_and_session_survives(self):
        d = Scripted([{"type": "text_delta", "text": "part"}, "HOLD"])
        mesh, client, runner = make(d)
        turn = claim(mesh, client)
        threading.Timer(0.1, mesh.request_stop, args=(turn["turn_id"],)).start()
        out = runner.run(turn, external_cancel=lambda: mesh.turns[turn["turn_id"]]["cancel_requested"])
        t = mesh.turns[turn["turn_id"]]
        self.assertEqual(out.state, "cancelled")
        self.assertTrue(d.seen_cancel)
        self.assertEqual(t["text"], "part")          # partial text kept

    def test_cancel_before_start_sends_only_final_cancelled(self):
        d = Scripted([{"type": "final", "stop_reason": "end_turn"}])
        mesh, client, runner = make(d)
        turn = claim(mesh, client)
        mesh.turns[turn["turn_id"]]["cancel_requested"] = True
        out = runner.run(turn)
        self.assertEqual(out.state, "cancelled")
        self.assertEqual(d.delivered, [])

    def test_delivery_crash_becomes_error_event(self):
        class Boom:
            def run(self, turn, cancelled):
                raise RuntimeError("tmux gone")
                yield
        mesh, client, runner = make(Boom())
        turn = claim(mesh, client)
        out = runner.run(turn)
        self.assertEqual(out.state, "failed")
        self.assertEqual(mesh.turns[turn["turn_id"]]["events"][-1]["code"], "delivery_failed")

    def test_lost_response_replays_same_seqs(self):
        d = Scripted([{"type": "text_delta", "text": "a"}, {"type": "final", "stop_reason": "end_turn"}])
        mesh, client, runner = make(d)
        turn = claim(mesh, client)
        mesh.fail_next = []          # the transient failure comes right after start
        orig = mesh.__call__
        calls = {"n": 0}

        def flaky(req, timeout=None):
            if req.full_url.endswith("/events") and calls["n"] == 0:
                calls["n"] += 1
                orig(req, timeout)          # applied by Mesh, response lost
                raise OSError("reset")
            return orig(req, timeout)
        client._mesh._open = lambda req, timeout=None: flaky(req, timeout)
        # the wrapper in ChatClient adds headers, so go through it
        client2 = ChatClient("https://mesh.test", "secret", opener=flaky)
        runner2 = ChatTurnRunner(client2, d, sleep=lambda s: None, batch_seconds=0.02)
        out = runner2.run(turn)
        t = mesh.turns[turn["turn_id"]]
        self.assertEqual(out.state, "done")
        self.assertEqual(t["text"], "a")             # not duplicated
        self.assertEqual([e["seq"] for e in t["events"]], [1, 2])

    def test_refused_batch_ends_with_error_at_the_same_seq(self):
        d = Scripted([{"type": "text_delta", "text": "a"}, {"type": "final", "stop_reason": "end_turn"}])
        mesh, client, runner = make(d)
        turn = claim(mesh, client)
        orig = mesh.__call__
        done = {"n": 0}

        def refuse_first_batch(req, timeout=None):
            if req.full_url.endswith("/events") and done["n"] == 0:
                done["n"] += 1
                return mesh._err(req, 422, "chat_bad_event")
            return orig(req, timeout)
        runner2 = ChatTurnRunner(ChatClient("https://mesh.test", "secret", opener=refuse_first_batch), d,
                                 sleep=lambda s: None, batch_seconds=0.02)
        out = runner2.run(turn)
        t = mesh.turns[turn["turn_id"]]
        self.assertEqual(out.state, "failed")
        self.assertEqual((t["state"], t["events"][-1]["seq"]), ("failed", 1))

    def test_unserved_agent_is_a_stable_error_code(self):
        mesh = FakeChatMesh()
        client = ChatClient("https://mesh.test", "secret", opener=mesh)
        with self.assertRaises(ChatError) as cm:
            client.claim(["other"], timeout=1)
        self.assertEqual(cm.exception.error_code, "chat_agent_not_served")
        self.assertEqual(cm.exception.context["agent_ids"], ["other"])

    def test_credential_is_not_sent_over_plain_http(self):
        with self.assertRaises(ValueError):
            ChatClient("http://mesh.test", "secret", opener=FakeChatMesh())

    def test_credential_never_in_repr_or_error(self):
        client = ChatClient("https://mesh.test", "secret", opener=FakeChatMesh())
        self.assertNotIn("secret", repr(client))


if __name__ == "__main__":
    unittest.main()
