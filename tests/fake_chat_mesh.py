"""In-process Mesh side of the Agent Chat runner protocol v1, used as the HTTP opener of a ChatClient.

Implements the turn state machine of the protocol doc closely enough for runner tests: claim,
start (commit point), events (seq order, replay, gaps, terminal), lease, release, cancel,
commands. Everything is guarded the way the doc orders the checks: protocol → agent → claim_token
→ replay → state → payload.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from email.message import Message

TERMINAL = ("done", "failed", "cancelled", "expired")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeChatMesh:
    def __init__(self, agents=("agent-1",), runner="runner-1"):
        self.agents, self.runner = set(agents), runner
        self.turns: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.claim_hides_commands = False       # production: an idle claim answers 204 though orders are pending
        self.acks: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.tokens_issued = 0
        self.fail_next: list[tuple[int, str]] = []        # injected (status, error_code) for the next calls
        self.n = 0
        self.history: dict[str, list[dict]] = {}       # thread -> messages, oldest first

    # ---- scenario setup -------------------------------------------------------------
    def queue_turn(self, text="hello", agent="agent-1", thread="thread-1", attachments=()) -> dict:
        self.n += 1
        t = {"turn_id": f"turn-{self.n}", "thread_id": thread, "agent_id": agent, "state": "queued", "attempt": 0,
             "claim_token": "", "last_seq": 0, "terminal_seq": None, "cancel_requested": False,
             "execution_epoch": 0, "lease_seconds": 60, "events": [], "text": "",
             "message": {"message_id": f"m{self.n}", "body": text, "attachments": list(attachments)},
             "requested_by": {"user_id": "u1", "display_name": "User"}, "settings": {"session_idle_minutes": 10},
             "thread_runtime": {"session_id": None}}
        self.turns[t["turn_id"]] = t
        return t

    def request_stop(self, turn_id: str) -> None:
        t = self.turns[turn_id]
        t["cancel_requested"] = True
        if t["state"] == "queued":
            t["state"] = "cancelled"

    # ---- opener ---------------------------------------------------------------------
    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        path = url.path.split("/api/v1/runner/chat", 1)[1]
        query = dict(urllib.parse.parse_qsl(url.query))
        body = json.loads(req.data) if req.data else {}
        self.requests.append((req.get_method(), path))
        if req.get_header("X-chat-protocol") != "1":
            return self._err(req, 400, "chat_protocol_unsupported")
        if req.get_header("X-agent-key") != "secret" or req.get_header("Authorization") or not req.get_header("X-chat-runner-id"):
            return self._err(req, 401, "unauthorized")
        if self.fail_next:
            status, code = self.fail_next.pop(0)
            return self._err(req, status, code)
        parts = path.strip("/").split("/")
        if parts[0] == "turns" and len(parts) == 2 and parts[1] == "active":
            return self._ok({"turns": [{k: t[k] for k in ("turn_id", "claim_token", "state", "last_seq")}
                                       for t in self.turns.values() if t["state"] in ("claimed", "streaming")]})
        if parts[0] == "commands" and len(parts) == 1:
            return self._ok({"commands": list(self.commands.values())})
        if parts[0] == "threads" and len(parts) == 3 and parts[2] == "history":
            msgs = self.history.get(parts[1], [])[-int(query.get("limit", 20)):]
            return self._ok({"messages": list(reversed(msgs))})      # newest first, as Mesh answers
        if parts[0] == "claim":
            return self._claim(req, query)
        if parts[0] == "turns" and len(parts) == 3:
            t = self.turns.get(parts[1])
            if t is None or t["agent_id"] not in self.agents:
                return self._err(req, 404, "chat_turn_not_found")
            return getattr(self, "_" + parts[2].replace("-", "_"))(req, t, body)
        if parts[0] == "commands" and len(parts) == 3:
            self.acks[parts[1]] = body
            self.commands.pop(parts[1], None)
            return self._ok({})
        return self._err(req, 404, "not_found")

    def _ok(self, obj, status=200):
        return Resp(json.dumps(obj).encode())

    def _err(self, req, status, code, ctx=None):
        body = json.dumps({"code": status, "error_code": code, "message": code, "context": ctx or {}}).encode()
        raise urllib.error.HTTPError(req.full_url, status, code, Message(), io.BytesIO(body))

    # ---- endpoints ------------------------------------------------------------------
    def _claim(self, req, q):
        asked = set(q.get("agents", "").split(","))
        if not asked <= self.agents:
            return self._err(req, 403, "chat_agent_not_served", {"agent_ids": sorted(asked - self.agents)})
        busy = {t["thread_id"] for t in self.turns.values() if t["state"] in ("claimed", "streaming")}
        out = []
        for t in self.turns.values():
            if t["state"] == "queued" and t["agent_id"] in asked and t["thread_id"] not in busy \
                    and len(out) < int(q.get("max", 1)):
                t.update(state="claimed", attempt=t["attempt"] + 1, claim_token=f"tok-{t['turn_id']}-{t['attempt'] + 1}",
                         execution_epoch=t["execution_epoch"] + 1)
                busy.add(t["thread_id"])
                out.append({k: t[k] for k in ("turn_id", "thread_id", "agent_id", "attempt", "claim_token",
                                              "execution_epoch", "lease_seconds", "message", "requested_by",
                                              "settings", "thread_runtime")})
        cmds = [] if self.claim_hides_commands else list(self.commands.values())
        if not out and not cmds:
            return Resp(b"")
        return self._ok({"protocol": 1, "turns": out, "commands": cmds})

    def _check(self, req, t, body, started=False):
        if body.get("claim_token") != t["claim_token"]:
            return self._err(req, 409, "chat_stale_claim")
        return None

    def _start(self, req, t, body):
        self._check(req, t, body)
        if t["state"] == "streaming":
            return self._ok({"state": "streaming", "cancel_requested": t["cancel_requested"], "last_seq": t["last_seq"]})
        if t["state"] != "claimed":
            return self._err(req, 409, "chat_turn_terminal", {"state": t["state"]})
        t["state"] = "streaming"
        return self._ok({"state": "streaming", "lease_until": "x", "cancel_requested": t["cancel_requested"],
                         "last_seq": 0, "execution_epoch": t["execution_epoch"]})

    def _lease(self, req, t, body):
        self._check(req, t, body)
        if t["state"] in TERMINAL:
            return self._err(req, 409, "chat_turn_terminal", {"state": t["state"]})
        return self._ok({"state": t["state"], "cancel_requested": t["cancel_requested"],
                         "execution_epoch": t["execution_epoch"]})

    def _session_token(self, req, t, body):
        self._check(req, t, body)
        if t["state"] == "claimed":
            return self._err(req, 409, "chat_not_started")
        if t["state"] in TERMINAL:
            return self._err(req, 409, "chat_turn_terminal", {"state": t["state"]})
        if t["cancel_requested"]:
            return self._err(req, 409, "chat_cancel_requested")
        self.tokens_issued += 1
        return self._ok({"token": f"tkn-{self.tokens_issued}", "execution_epoch": t["execution_epoch"],
                         "generation": self.tokens_issued, "kind": "chat_session", "ttl_seconds": 300})

    def _release(self, req, t, body):
        self._check(req, t, body)
        if t["state"] == "streaming":
            return self._err(req, 409, "chat_turn_started")
        t["state"] = "queued"
        t["claim_token"] = ""
        return self._ok({"state": "queued"})

    def _events(self, req, t, body):
        self._check(req, t, body)
        evs = body["events"]
        if all(e["seq"] <= t["last_seq"] for e in evs):
            return self._ok({"applied_seq": t["last_seq"], "state": t["state"], "cancel_requested": t["cancel_requested"]})
        if t["state"] in TERMINAL:
            return self._err(req, 409, "chat_turn_terminal", {"state": t["state"], "terminal_seq": t["terminal_seq"]})
        if t["state"] == "claimed":
            only_cancel = len(evs) == 1 and evs[0]["type"] == "final" and evs[0].get("stop_reason") == "cancelled" \
                and evs[0]["seq"] == 1
            if not (only_cancel or all(e["type"] == "error" for e in evs)):
                return self._err(req, 409, "chat_not_started")
        if evs[0]["seq"] != t["last_seq"] + 1 and evs[0]["seq"] > t["last_seq"]:
            return self._err(req, 409, "chat_seq_gap", {"expected_seq": t["last_seq"] + 1})
        for e in evs:
            if e["seq"] <= t["last_seq"]:
                continue
            t["last_seq"] = e["seq"]
            t["events"].append(e)
            if e["type"] == "text_delta":
                t["text"] += e["text"]
            elif e["type"] == "final":
                if e.get("text") is not None:
                    t["text"] = e["text"]
                t["state"] = "cancelled" if e.get("stop_reason") == "cancelled" else "done"
                t["terminal_seq"] = e["seq"]
                break
            elif e["type"] == "error":
                t["state"], t["terminal_seq"] = "failed", e["seq"]
                break
        return self._ok({"applied_seq": t["last_seq"], "state": t["state"],
                         "cancel_requested": t["cancel_requested"], "ignored": []})
