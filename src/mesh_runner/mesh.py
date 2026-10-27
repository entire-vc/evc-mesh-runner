"""Minimal Mesh REST client for one agent identity.

Only the agent API is used (``X-Agent-Key``), so the runner works on any Mesh
instance without extension-specific endpoints. Keys are never logged.
"""
from __future__ import annotations

import base64
import datetime
import email.utils
import json
import math
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT = 30
# a GET is safe to repeat: a lone transport failure (timeout, TLS handshake, DNS) is retried
# before the tick is failed; writes are never repeated here
GET_RETRY_DELAYS = (1.0, 3.0)
# 429 from the rate-limit middleware (it sends Retry-After) is answered before any handler runs, so
# repeating is safe. Sleep Retry-After (capped) plus jitter; a separate budget from transport retries.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_MAX_SLEEP = 30.0
RATE_LIMIT_DEFAULT_SLEEP = 2.0


def _parse_retry_after(value: Any) -> float | None:
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP-date; None if absent/invalid."""
    if value is None:
        return None
    try:
        secs = float(value)
    except (TypeError, ValueError):
        try:
            dt = email.utils.parsedate_to_datetime(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            secs = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except (TypeError, ValueError, IndexError):
            return None
    return max(0.0, secs) if math.isfinite(secs) else None


class MeshError(Exception):
    def __init__(self, status: int, message: str, body: Any = None, retry_after: float | None = None,
                 endpoint: str = ""):
        super().__init__(f"HTTP {status}: {message}" + (f" [{endpoint}]" if endpoint else "")
                         + (f" retry-after={retry_after:g}s" if retry_after is not None else ""))
        self.status = status
        self.body = body
        self.retry_after = retry_after
        self.endpoint = endpoint


@dataclass
class Checkout:
    task_id: str
    token: str
    generation: int | None
    expires_at: str
    session_id: str
    request_id: str


class MeshClient:
    def __init__(self, base_url: str, agent_key: str, timeout: int = DEFAULT_TIMEOUT, opener=None,
                 basic_auth: str = "", bearer=None):
        self.base = base_url.rstrip("/") + "/api/v1"
        self._key = agent_key
        # a self-hosted Mesh behind an HTTP basic-auth gate: "user:pass" in Authorization,
        # the agent key stays in X-Agent-Key, so the two never collide
        self._basic = ("Basic " + base64.b64encode(basic_auth.encode()).decode()) if basic_auth else ""
        # an extension actor token (``ext_...``): a callable, so a rotated token is read at once
        self._bearer = bearer
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def __repr__(self) -> str:  # never show the key
        return f"MeshClient({self.base!r})"

    # -- transport -------------------------------------------------------------
    def request(self, method: str, path: str, body: Any = None, query: dict | None = None) -> Any:
        delays = list(GET_RETRY_DELAYS) if method == "GET" else []
        limited = 0
        while True:
            try:
                return self._request(method, path, body, query)
            except MeshError as e:
                # a write is repeated only when the server named a Retry-After: that header comes from
                # the rate-limit middleware, a handler-level 429 might have applied side effects
                if (e.status == 429 and limited < RATE_LIMIT_RETRIES
                        and (method == "GET" or e.retry_after is not None)):
                    limited += 1
                    wait = e.retry_after if e.retry_after is not None else RATE_LIMIT_DEFAULT_SLEEP
                    wait = min(wait, RATE_LIMIT_MAX_SLEEP)
                    time.sleep(wait + random.uniform(0, 0.5 + wait * 0.1))
                elif e.status == 0 and delays:
                    time.sleep(delays.pop(0))
                else:
                    raise

    def bounded_get(self, path: str, *, timeout: float) -> Any:
        """One diagnostic GET, never transport/429 retries or sleeps.

        A reaped child isolates DNS and response reads behind the residual wall
        deadline. Credentials travel only on stdin, never argv or error output.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise MeshError(0, "diagnostic deadline")
        deadline = time.monotonic() + timeout
        # Reserve cleanup time inside the caller's shared diagnostic budget.
        if timeout <= .1:
            raise MeshError(0, "diagnostic deadline")
        try:
            bearer = self._bearer() if self._bearer else ""
        except (OSError, RuntimeError):
            raise MeshError(0, "diagnostic credential unavailable") from None
        payload = json.dumps({"base": self.base[:-7], "key": self._key,
                              "basic": self._basic, "bearer": bearer, "path": path,
                              "timeout": min(timeout - .1, self.timeout)}).encode()
        try:
            child = subprocess.Popen([sys.executable, "-m", "mesh_runner.diagnostic_get"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        except OSError:
            raise MeshError(0, "diagnostic process unavailable") from None
        try:
            remaining = deadline - time.monotonic() - .1
            if remaining <= 0:
                raise subprocess.TimeoutExpired(child.args, timeout)
            output, _ = child.communicate(payload, timeout=remaining)
        except (subprocess.TimeoutExpired, OSError):
            raise MeshError(0, "diagnostic deadline") from None
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=.05)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()             # reap; never leave a diagnostic writer behind
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if time.monotonic() >= deadline or child.returncode or len(output) > 1048640:
            raise MeshError(0, "diagnostic response unavailable")
        try:
            result = json.loads(output)
            if result["status"] != 200:
                raise MeshError(int(result["status"]), "diagnostic GET failed")
            return result["body"]
        except (ValueError, KeyError, TypeError, RecursionError):
            raise MeshError(0, "diagnostic response unavailable") from None

    def _bounded_get_request(self, path: str, timeout: float) -> Any:
        """Child-only, single socket request; parent enforces the wall deadline."""
        return self._request("GET", path, timeout=timeout, max_bytes=1024 * 1024)

    def _request(self, method: str, path: str, body: Any = None, query: dict | None = None,
                 *, timeout: float | None = None, max_bytes: int | None = None) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/json",
            **({"X-Agent-Key": self._key} if not self._bearer else {}),
            **({"Authorization": "Bearer " + self._bearer()} if self._bearer
               else {"Authorization": self._basic} if self._basic else {}),
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        try:
            with self._open(req, timeout=self.timeout if timeout is None else timeout) as resp:
                raw = resp.read() if max_bytes is None else resp.read(max_bytes + 1)
                if max_bytes is not None and len(raw) > max_bytes:
                    raise MeshError(0, "diagnostic response bound")
        except urllib.error.HTTPError as e:
            raw = (e.read() if max_bytes is None else e.read(max_bytes + 1)) or b""
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw.decode(errors="replace")[:500]
            msg = parsed.get("message") if isinstance(parsed, dict) else str(parsed)[:200]
            retry_after = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
            raise MeshError(e.code, msg or e.reason, parsed, retry_after=retry_after,
                            endpoint=f"{method} {path}") from None
        except (OSError, ValueError) as e:      # URLError, timeouts, resets
            raise MeshError(0, f"{type(e).__name__}: {str(e)[:200]}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise MeshError(0, "response is not JSON") from None

    # -- agent -------------------------------------------------------------------
    def me(self) -> dict:
        return self.request("GET", "/agents/me")

    def my_tasks(self, status_category: str) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            res = self.request("GET", "/agents/me/tasks",
                               query={"status_category": status_category, "page": page, "page_size": 100})
            items = _items(res)
            out.extend(items)
            if not (isinstance(res, dict) and res.get("has_more")) or not items:
                return out
            page += 1

    # -- tasks -------------------------------------------------------------------
    def task(self, task_id: str) -> dict:
        return self.request("GET", f"/tasks/{task_id}")

    def comments(self, task_id: str, limit: int = 40) -> list[dict]:
        """At least the newest ``limit`` comments, oldest first. Mesh pages
        comments oldest-first, so the newest live on the last page; a short last
        page is topped up from the one before it."""
        def page(n: int) -> dict:
            res = self.request("GET", f"/tasks/{task_id}/comments",
                               query={"limit": limit, "page_size": limit, "page": n})
            return res if isinstance(res, dict) else {"items": res or []}
        first = page(1)
        pages = int(first.get("total_pages") or 1)
        if pages <= 1:
            items = _items(first)
        else:
            items = _items(page(pages))
            if len(items) < limit and pages > 1:
                items = _items(page(pages - 1)) + items
        return sorted(items, key=lambda c: c.get("created_at") or "")

    def add_comment(self, task_id: str, body: str) -> dict:
        return self.request("POST", f"/tasks/{task_id}/comments", {"body": body})

    def dependencies(self, task_id: str) -> dict:
        return self.request("GET", f"/tasks/{task_id}/dependencies") or {}

    def patch_task(self, task_id: str, fields: dict) -> dict:
        return self.request("PATCH", f"/tasks/{task_id}", fields)

    def statuses(self, project_id: str) -> list[dict]:
        return _items(self.request("GET", f"/projects/{project_id}/statuses"))

    def move(self, task_id: str, status_id: str, expected_version: int | None = None,
             expected_status_id: str | None = None) -> dict:
        """Move a task. With ``expected_*`` the server writes only if the card is still as read
        (atomic; otherwise 409 cas_conflict and no write)."""
        body = {"status_id": status_id}
        if expected_version is not None:
            body["expected_version"] = int(expected_version)
        if expected_status_id:
            body["expected_status_id"] = expected_status_id
        return self.request("POST", f"/tasks/{task_id}/move", body)

    def session_report(self, task_id: str, tokens_in: int, tokens_out: int, model: str, cost: float) -> None:
        """Spend of one turn on the agent's Mesh record (POST /agents/me/sessions/report)."""
        body = {"tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "model": model or "",
                "estimated_cost": round(float(cost), 4)}
        if task_id:
            body["task_id"] = task_id
        self.request("POST", "/agents/me/sessions/report", body)

    def vcs_links(self, task_id: str) -> list[dict]:
        """Merge requests, commits and branches linked to the task. Mesh keeps each
        link's state current from provider webhooks: ``status`` (open/merged/closed)
        with ``status_synced_at``, ``pipeline_status`` with ``pipeline_updated_at``."""
        res = self.request("GET", f"/tasks/{task_id}/vcs-links")
        if isinstance(res, dict) and isinstance(res.get("vcs_links"), list):
            return res["vcs_links"]
        return _items(res)

    def put_runtime_state(self, task_id: str, state: dict) -> None:
        self.request("PUT", f"/tasks/{task_id}/runtime-state", state)

    def delete_runtime_state(self, task_id: str) -> None:
        try:
            self.request("DELETE", f"/tasks/{task_id}/runtime-state")
        except MeshError as e:
            if e.status not in (403, 404, 409, 422):   # closed or no longer ours: nothing to clear
                raise

    def add_dependency(self, task_id: str, depends_on: str) -> dict:
        return self.request("POST", f"/tasks/{task_id}/dependencies",
                            {"depends_on_task_id": depends_on, "dependency_type": "blocks"}) or {}

    def delete_dependency(self, task_id: str, edge_id: str) -> None:
        try:
            self.request("DELETE", f"/tasks/{task_id}/dependencies/{edge_id}")
        except MeshError as e:
            if e.status not in (404, 410):
                raise

    def task_by_short(self, short: str) -> dict:
        t = self.request("GET", f"/tasks/by-short-id/{short}")
        return t.get("task", t) if isinstance(t, dict) else {}

    def create_task(self, project_id: str, fields: dict) -> dict:
        t = self.request("POST", f"/projects/{project_id}/tasks", fields) or {}
        return t.get("task", t) if isinstance(t, dict) else {}

    def find_tasks(self, project_id: str, search: str) -> list[dict]:
        """Cards of a project whose title or description contains ``search`` (any status)."""
        return _items(self.request("GET", f"/projects/{project_id}/tasks",
                                   query={"search": search, "page_size": 50}))

    def assign(self, task_id: str, agent_id: str) -> None:
        self.request("POST", f"/tasks/{task_id}/assign",
                     {"assignee_id": agent_id, "assignee_type": "agent", "source": "rule"})

    def mentions(self, workspace_id: str) -> list[dict]:
        """Unseen @-mentions of this agent in task comments (GET /me/mentions), newest
        100; older ones come once these are marked seen."""
        q = {"workspace_id": workspace_id, "seen": "false", "limit": 100}
        return _items(self.request("GET", "/me/mentions", query=q))

    def mention_seen(self, comment_id: str) -> None:
        self.request("POST", f"/me/mentions/{comment_id}/seen")

    def workspace_recipients(self, workspace_id: str, handle: str) -> list[dict]:
        """Authoritative mention identities, one GET with a five-second wall bound.

        A full result may conceal a collision beyond the search limit. Neither a
        truncated nor a malformed response proves that a recipient is unique.
        """
        if not workspace_id:
            raise MeshError(0, "recipient workspace unavailable")
        query = urllib.parse.urlencode({"q": handle, "limit": 50})
        rows = self.bounded_get(f"/workspaces/{workspace_id}/mentionables?{query}", timeout=5)
        if (not isinstance(rows, list) or len(rows) >= 50
                or any(not isinstance(row, dict)
                       or any(not isinstance(row.get(field), str) or not row[field]
                              for field in ("id", "kind", "slug")) for row in rows)):
            raise MeshError(0, "recipient roster incomplete")
        return rows

    def workspace_agents(self, workspace_id: str) -> list[dict]:
        out, page = [], 1
        while True:
            res = self.request("GET", f"/workspaces/{workspace_id}/agents", query={"page": page, "page_size": 200})
            items = _items(res)
            out += items
            more = isinstance(res, dict) and (res.get("has_more") or page < int(res.get("total_pages") or 1))
            if not items or not more:
                return out
            page += 1

    def subtasks(self, task_id: str) -> list[dict]:
        return _items(self.request("GET", f"/tasks/{task_id}/subtasks"))

    # -- checkout (single writer per task) ----------------------------------------
    def checkout(self, task_id: str, session_id: str, request_id: str, ttl_minutes: int = 120) -> Checkout:
        res = self.request("POST", f"/tasks/{task_id}/checkout",
                           {"session_id": session_id, "request_id": request_id, "ttl_minutes": ttl_minutes})
        token = (res or {}).get("checkout_token")
        if not token:
            raise MeshError(0, "checkout returned no token", res)
        gen = res.get("generation")
        return Checkout(task_id, token, int(gen) if gen is not None else None,
                        res.get("expires_at", ""), session_id, request_id)

    def heartbeat(self, co: Checkout, ttl_minutes: int = 120) -> None:
        body = {"checkout_token": co.token, "session_id": co.session_id, "ttl_minutes": ttl_minutes}
        if co.generation is not None:
            body["generation"] = co.generation
        self.request("PATCH", f"/tasks/{co.task_id}/checkout", body)

    def release(self, co: Checkout) -> None:
        body = {"checkout_token": co.token, "session_id": co.session_id}
        if co.generation is not None:
            body["generation"] = co.generation
        try:
            self.request("DELETE", f"/tasks/{co.task_id}/checkout", body)
        except MeshError as e:
            if e.status not in (403, 404, 409):  # already released or taken over
                raise


def _items(res: Any) -> list[dict]:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ("items", "tasks", "comments", "statuses", "data"):
            if isinstance(res.get(key), list):
                return res[key]
    return []
