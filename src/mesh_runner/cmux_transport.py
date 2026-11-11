"""Local cmux transport of the strict existing-session metadata receiver (default off; ``transport = "cmux"``).

Why a contract of its own: in cmux 0.65.2 every terminal input verb (``surface.send_text``, ``surface.send_key``,
``terminal.paste``) goes through ``TerminalPanel``'s explicit-input wrappers, which call
``resumeForExplicitInputIfNeeded()`` first: a hibernated agent surface is RESUMED (a new native process) before the bytes
are written, and a surface without a live runtime QUEUES the input. ``agent.message.send`` only queues a message per
recipient surface, without the expected native process, and any later producer on that surface may claim it. None of
these can promise "these bytes reached exactly this native process, or nothing happened". Unknown params are ignored by
those verbs, so adding guard params to them would silently degrade on an older cmux. Hence one NEW method, which an older
cmux answers with ``method_not_found`` (= refusal, zero input):

``surface.exact_input`` — contract ``cmux.exact_input.v1`` (additive; patches 0001 cmux + 0002 its ghostty in ``contrib/cmux/``)
  request params (all required unless noted):
    contract              "cmux.exact_input.v1"
    mode                  "probe" (checks only, no input) | "stage" (bracketed paste ONLY, NO Enter) | "submit" (Enter only)
    workspace_id          workspace UUID          — exact; no focus, index or caller-context fallback
    surface_id            surface UUID            — exact
    expected_tty          "/dev/ttysNNN"          — must equal the surface's CURRENT-runtime reported tty
    expected_pid          native producer pid     — alive, on that tty, foreground process group of it
    expected_pid_start    its birth time, epoch seconds (kernel pbi_start_tvsec)
    no_resume, no_queue   literal true            — the method refuses anything else
    text                  stage only
    expected_runtime_generation   submit only: the generation the stage answered with
  every check runs in ONE main-actor turn together with the write, on the raw surface (never the panel wrapper):
    surface missing/closed -> exact_target_missing; panel/registry owners differ -> exact_target_replacing;
    hibernated -> exact_target_hibernated (never resumed); no live runtime -> exact_surface_unavailable (never queued);
    tty not reported by the current runtime -> exact_tty_unreported; other tty -> exact_tty_mismatch;
    pid gone -> exact_producer_gone; other birth time -> exact_producer_changed; other tty -> exact_producer_not_on_tty;
    not the foreground group -> exact_producer_not_foreground; generation moved -> exact_generation_changed;
    probe/stage while the terminal has no bracketed paste (DECSET 2004) -> exact_paste_unbracketed: the metadata block
    is multi-line and is only ever written FRAMED (ghostty ``ghostty_surface_text_bracketed_only``, patch 0002), so an
    interior newline can never submit a line before the separate Enter;
    a clipboard read would defer the bytes -> exact_queued_not_received (nothing written, nothing kept);
    malformed request (non-literal UUID, no_resume/no_queue not literal true, ESC/NUL in text) -> exact_invalid_params.
  answer: {"contract","mode","delivery": "probed"|"received","workspace_id","surface_id","tty","producer_pid",
           "producer_start","runtime_generation","resumed": false,"queued": false}

What this module adds on top (mirrors the accepted iTerm transport): the producer is also re-proved locally (``ps``) before
the stage and between stage and Enter; the answer must echo the exact pin, ``resumed`` and ``queued`` must be literally
false, and ``delivery`` literally ``received`` — a queue acknowledgement is never ``received``. The stage and the Enter
are two calls; if the producer or generation changes in between, no Enter is sent and the delivery is ``uncertain``
(inert text may sit unsubmitted in the prompt). Window focus is never consulted.

Transport client: the bundled ``cmux`` CLI's ``rpc`` verb (the socket access mode ``cmuxOnly`` admits exactly that
client; nothing here opens the socket itself, asks for a grant or changes cmux settings). Values travel as one JSON argv
element, never through a shell. Nothing here uses SSH/CDP, keystrokes, the frontmost window, a new window/surface, a
resume, the hook message queue or the producer's lifecycle.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass

from .iterm_transport import process_info

log = logging.getLogger("mesh_runner.cmux_transport")

CONTRACT = "cmux.exact_input.v1"
METHOD = "surface.exact_input"
DEFAULT_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"
TIMEOUT = 15
SUBMIT_PAUSE = 0.15                                   # cmux's own mobile paste keeps paste and Enter in separate input turns
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
TTY = re.compile(r"^/dev/ttys\d{3,}$")
GENERATION = re.compile(r"^\d{1,20}$")
EXACT_CODE = re.compile(r"\bexact_([a-z_]+)\b")
_ROUTING_ENV = ("CMUX_SURFACE_ID", "CMUX_WORKSPACE_ID", "CMUX_PANEL_ID", "CMUX_TAB_ID", "CMUX_WINDOW_ID")


@dataclass(frozen=True)
class CmuxPin:
    workspace_id: str            # cmux workspace UUID
    surface_id: str              # cmux terminal surface UUID
    tty: str                     # "/dev/ttysNNN" of that surface's current runtime
    producer_pid: int            # the native producer reading that terminal
    producer_started: str        # `ps -o lstart=` baseline of that pid


def valid_pin(pin: CmuxPin) -> str:
    """"" when well formed, else the typed reason (a malformed pin never reaches cmux)."""
    if not UUID_RE.match(pin.workspace_id):
        return "cmux_workspace_id_invalid"
    if not UUID_RE.match(pin.surface_id):
        return "cmux_surface_id_invalid"
    if not TTY.match(pin.tty):
        return "cmux_tty_invalid"
    if not isinstance(pin.producer_pid, int) or pin.producer_pid <= 0:
        return "cmux_producer_pid_invalid"
    return ""


def start_epoch(lstart: str) -> int | None:
    """``ps -o lstart=`` (local time, C locale) -> epoch seconds, the unit of the kernel birth time cmux compares."""
    try:
        return int(time.mktime(time.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError):
        return None


def _reason(res: subprocess.CompletedProcess) -> str:
    err = (res.stderr or "") + "\n" + (res.stdout or "")
    m = EXACT_CODE.search(err)
    if m:
        return m.group(1)
    if "method_not_found" in err or "Unknown method" in err:
        return "cmux_exact_input_unsupported"            # an older cmux: the guarded verb does not exist, nothing was sent
    return "cmux_unavailable"


class CmuxAPI:
    """The one ``cmux rpc surface.exact_input`` call, behind a seam (tests use a fake CLI binary at the same boundary)."""

    def __init__(self, binary: str = "", socket_path: str = ""):
        self.binary, self.socket_path = binary or DEFAULT_CLI, socket_path

    def call(self, params: dict) -> tuple[dict | None, str]:
        argv = [self.binary] + (["--socket", self.socket_path] if self.socket_path else []) + [
            "rpc", METHOD, json.dumps(params, separators=(",", ":"))]
        env = {k: v for k, v in os.environ.items() if k not in _ROUTING_ENV}   # never the caller's surface context
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            return None, "cmux_timeout"
        except OSError:
            return None, "cmux_unavailable"
        if res.returncode != 0:
            return None, _reason(res)
        try:
            out = json.loads(res.stdout)
        except ValueError:
            return None, "cmux_response_invalid"
        return (out, "") if isinstance(out, dict) else (None, "cmux_response_invalid")

    def _params(self, pin: CmuxPin, mode: str, **extra) -> dict | None:
        born = start_epoch(pin.producer_started)
        if born is None:
            return None
        return {"contract": CONTRACT, "mode": mode, "workspace_id": pin.workspace_id, "surface_id": pin.surface_id,
                "expected_tty": pin.tty, "expected_pid": pin.producer_pid, "expected_pid_start": born,
                "no_resume": True, "no_queue": True, **extra}

    def exact(self, pin: CmuxPin, mode: str, **extra) -> tuple[str, str]:
        """``(runtime_generation, "")`` when cmux proved (and for stage/submit wrote) exactly the pinned target, else
        ``("", reason)``."""
        params = self._params(pin, mode, **extra)
        if params is None:
            return "", "producer_start_unknown"
        res, reason = self.call(params)
        if reason:
            return "", reason
        return answer_matches(res, pin, mode, params["expected_pid_start"], extra.get("expected_runtime_generation"))


def answer_matches(res: dict, pin: CmuxPin, mode: str, born: int, generation: str | None = None) -> tuple[str, str]:
    """The answer must echo the exact pin and say literally probed/received, not resumed, not queued."""
    same = lambda a, b: isinstance(a, str) and a.lower() == b.lower()
    gen = res.get("runtime_generation")
    if (res.get("contract") != CONTRACT or res.get("mode") != mode
            or not same(res.get("workspace_id"), pin.workspace_id) or not same(res.get("surface_id"), pin.surface_id)
            or res.get("tty") != pin.tty or res.get("producer_pid") != pin.producer_pid or res.get("producer_start") != born
            or not isinstance(gen, str) or not GENERATION.match(gen)):
        return "", "cmux_response_mismatch"
    if res.get("resumed") is not False:
        return "", "cmux_resumed"
    if res.get("queued") is not False:
        return "", "queued_not_received"
    if res.get("delivery") != ("probed" if mode == "probe" else "received"):
        return "", "queued_not_received"
    if generation is not None and gen != generation:
        return "", "generation_changed"
    return gen, ""


def check_local(pin: CmuxPin, info=process_info) -> str:
    """The producer as this host's kernel sees it: alive, exact birth time, on the pinned tty, foreground group."""
    pgid, tpgid, ps_tty, started = info(pin.producer_pid)
    if not started:
        return "producer_gone"
    if not " ".join(pin.producer_started.split()):
        return "producer_start_unknown"
    if started != " ".join(pin.producer_started.split()):
        return "producer_changed"
    if "/dev/" + ps_tty != pin.tty:
        return "producer_not_on_tty"
    if pgid != tpgid:
        return "producer_not_foreground"
    return ""


def check_cmux(api: CmuxAPI, pin: CmuxPin, info=process_info) -> str:
    """"" when surface, tty and producer are exactly the pinned ones (local kernel AND cmux probe), else the reason."""
    reason = valid_pin(pin) or check_local(pin, info)
    if reason:
        return reason
    _, reason = api.exact(pin, "probe")
    return reason


class CmuxSession:
    """The one existing native session in its cmux surface, found by PIN and never created or resumed. Same surface as
    ``meta_delivery.TmuxSession`` (known/target/paste), so receiver, journal and ledger above it are unchanged."""

    def __init__(self, api: CmuxAPI, pin: CmuxPin, session_id: str, known, info=process_info, pause=time.sleep):
        self.api, self.pin, self.session_id, self._known, self._info, self._pause = api, pin, session_id, known, info, pause

    def known(self) -> bool:
        try:
            return bool(self._known(self.session_id))
        except OSError:
            return False

    def target(self) -> str | None:
        reason = check_cmux(self.api, self.pin, self._info)
        if reason:
            log.warning("session receiver: cmux target not as pinned (%s); no injection", reason)
            return None
        return self.pin.surface_id

    def paste(self, target: str, text: str) -> bool:
        """True only when cmux answered ``received`` for the stage AND for the Enter on the same runtime generation.
        False = nothing or unsubmitted text: the receiver journals it ``uncertain`` and never repeats it."""
        if target != self.pin.surface_id or not text or "\x1b" in text or "\x00" in text:
            return False                                           # text must not carry its own paste terminator
        reason = check_local(self.pin, self._info)
        if reason:
            log.warning("session receiver: cmux producer changed before stage (%s); no injection", reason)
            return False
        generation, reason = self.api.exact(self.pin, "stage", text=text)
        if reason:
            log.warning("session receiver: cmux stage refused (%s)", reason)    # never the text, never raw stderr
            return False
        self._pause(SUBMIT_PAUSE)
        reason = check_local(self.pin, self._info)
        if reason:
            log.warning("session receiver: cmux producer changed between paste and Enter (%s); no Enter", reason)
            return False
        _, reason = self.api.exact(self.pin, "submit", submit_key="return", expected_runtime_generation=generation)
        if reason:
            log.warning("session receiver: cmux submit refused (%s); text may sit unsubmitted", reason)
            return False
        return True
