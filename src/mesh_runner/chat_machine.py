"""Machine-side pieces of a chat: its window, its files, the prompt it is given.

- `stop_window` ends the chat's tmux window (the session process), keeping the transcript;
- `delete_chat` removes everything a chat left on the machine: window, run metadata, the
  harness transcript (Claude project file / Codex rollout) and the temp attachment directory;
- `fetch_attachments` downloads the current message's files into the chat's temp directory
  (names sanitized, size-capped, refreshed link on expiry);
- `build_prompt` turns a turn into the text pasted into the session; when the session cannot be
  resumed, a compact history of the last messages comes first.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .chat_delivery import window_key
from .chat_protocol import ChatClient, ChatError
from .interactive import Tmux, agent_dir, forget, read_meta, session_name

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
HISTORY_CHARS = 6000


def chat_dir(state_dir: Path, thread_id: str) -> Path:
    return Path(state_dir) / "chat" / window_key(thread_id)


def stop_window(tmux: Tmux, state_dir: Path, agent: str, thread_id: str) -> bool:
    """True when the chat had a live window. Metadata is kept for a later resume."""
    key, session = window_key(thread_id), session_name(agent)
    existed = key in tmux.windows(session)
    if existed:
        tmux.kill_window(session, key)
    return existed


def _rmtree(path) -> None:
    """Remove a tree; a leftover is an error (the caller must not report the chat as deleted)."""
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        raise OSError(f"could not remove {os.path.basename(str(path))}")


def delete_chat(tmux: Tmux, state_dir: Path, agent: str, thread_id: str, env: dict | None = None) -> bool:
    """True when anything of the chat was found on this machine."""
    env = env or os.environ
    key, directory = window_key(thread_id), agent_dir(state_dir, agent)
    found = stop_window(tmux, state_dir, agent, thread_id)
    meta = read_meta(directory, key)
    victims: list[str] = []
    side_dirs: list[str] = []
    sid = str(meta.get("session_id") or "")
    if meta.get("transcript"):
        victims.append(str(meta["transcript"]))
        if sid and re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):   # the account may have changed since: side data sits next to the transcript
            side_dirs.append(os.path.join(os.path.dirname(str(meta["transcript"])), sid))
    if not sid:                                         # meta lost: the id is deterministic per chat
        from .chat_host import session_id_for
        sid = session_id_for(thread_id)
    if sid and re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
        home = env.get("HOME") or os.path.expanduser("~")
        claude = env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
        codex = env.get("CODEX_HOME") or os.path.join(home, ".codex")
        victims += glob.glob(os.path.join(claude, "projects", "*", f"{sid}.jsonl"))
        victims += glob.glob(os.path.join(codex, "sessions", "**", f"rollout-*{sid}*.jsonl"), recursive=True)
    for path in dict.fromkeys(victims):
        try:
            os.unlink(path)
            found = True
        except FileNotFoundError:
            pass
        # any other failure propagates: the delete is not acknowledged and Mesh retries it
    if sid and re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):   # Claude's side data: subagent transcripts, tool results
        side_dirs += glob.glob(os.path.join(claude, "projects", "*", sid))
        for side in dict.fromkeys(side_dirs):
            if os.path.isdir(side):
                _rmtree(side)
                found = True
    logs = Path(state_dir) / "chat" / "logs" / key
    if logs.exists():
        _rmtree(logs)
        found = True
    tmp = chat_dir(state_dir, thread_id)
    if tmp.exists():
        _rmtree(tmp)
        found = True
    if meta:
        found = True
    forget(directory, key)
    return found


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):               # a presigned link does not redirect; one that does is hostile
        raise urllib.error.URLError("redirect refused")


_NO_REDIRECT = urllib.request.build_opener(_Refuse)


def _download(url: str, dest: Path, limit: int) -> None:
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("attachment link must be https")
    with _NO_REDIRECT.open(url, timeout=60) as resp:          # presigned, no credentials sent
        size = 0
        with open(dest, "wb") as out:
            while chunk := resp.read(65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError("attachment over the size limit")
                out.write(chunk)


def fetch_attachments(client: ChatClient, turn: dict, state_dir: Path,
                      download: Callable[[str, Path, int], None] = _download) -> list[tuple[dict, Path | None]]:
    """(attachment, local path) pairs; a file that could not be fetched has path None."""
    files = (turn.get("message") or {}).get("attachments") or []
    if not files:
        return []
    directory = chat_dir(state_dir, turn["thread_id"]) / "files"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    limit = int((turn.get("settings") or {}).get("max_attachment_mb") or 25) * 1024 * 1024
    out = []
    for i, a in enumerate(files):
        name = SAFE_NAME.sub("_", os.path.basename(str(a.get("file_name") or f"file{i}")))[:80] or f"file{i}"
        dest = directory / f"{i}-{name}"
        path: Path | None = dest
        try:
            try:
                download(a["download_url"], dest, limit)
            except OSError:                                  # link expired: ask for a fresh one once
                url = client.download_url(turn["turn_id"], a["attachment_id"])["download_url"]
                download(url, dest, limit)
        except (ChatError, OSError, ValueError, KeyError):
            dest.unlink(missing_ok=True)
            path = None
        out.append((a, path))
    return out


def build_prompt(turn: dict, files: list[tuple[dict, Path | None]], history: list[dict] | None = None) -> str:
    parts = []
    if history:
        lines, used = [], 0
        for m in reversed(history):                           # newest first, cut by budget, then restored
            line = f"{'User' if m.get('role') == 'user' else 'You'}: {m.get('body') or ''}"
            used += len(line)
            if used > HISTORY_CHARS:
                break
            lines.append(line)
        parts.append("Earlier in this conversation (the session was restarted):\n" + "\n".join(reversed(lines)))
    who = (turn.get("requested_by") or {}).get("display_name")
    body = (turn.get("message") or {}).get("body") or ""
    parts.append(f"{who}: {body}" if who else body)
    notes = []
    for a, path in files:
        label = SAFE_NAME.sub("_", os.path.basename(str(a.get("file_name") or "file")))[:80] or "file"
        notes.append(f"[attachment {label}: {path}]" if path else f"[attachment {label}: could not be downloaded]")
    return "\n\n".join(parts + (["\n".join(notes)] if notes else []))
