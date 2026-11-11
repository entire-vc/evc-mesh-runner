"""Every write of an attempt's state goes through ``lifecycle.AttemptStateMachine``.

The guard parses each module under ``src/mesh_runner`` (``ast``, not text) and fails on a call to a store's
``update(`` / ``update_with_marks(`` that passes a ``state=`` keyword anywhere but in ``lifecycle.py`` (the state
machine) and ``store.py`` (the store itself). ``dict.update(state=...)`` on records, journals and copies is not a
store write and is not flagged.
"""
import ast
import unittest
from pathlib import Path

from mesh_runner import lifecycle

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "mesh_runner"
ALLOWED = {"lifecycle.py", "store.py"}


def _receiver(node: ast.expr) -> str:
    """The last name of a call's receiver: ``self.store`` -> ``store``, ``store`` -> ``store``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def state_writes(source: str) -> list[int]:
    """Line numbers of store state writes in ``source``. A call is a store write when it is
    ``update_with_marks(...)`` (only the store has it), or ``update(...)`` on a receiver named like a store
    (``store``, ``self.store``, ``self._store``) or with a positional argument (the store takes the attempt
    positionally; ``dict.update(state=...)`` does not)."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("update", "update_with_marks"):
            continue
        if not any(k.arg == "state" for k in node.keywords):
            continue
        store_like = (node.func.attr == "update_with_marks" or "store" in _receiver(node.func.value).lower()
                      or bool(node.args))
        if store_like:
            hits.append(node.lineno)
    return hits


class StateWritesGoThroughTheStateMachine(unittest.TestCase):
    def test_no_module_writes_a_state_around_the_state_machine(self):
        modules = sorted(PACKAGE.rglob("*.py"))
        self.assertIn(PACKAGE / "runner.py", modules)
        offenders = []
        for path in modules:
            if path.name in ALLOWED and path.parent == PACKAGE:
                continue
            for line in state_writes(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(PACKAGE)}:{line}")
        self.assertEqual(offenders, [], "write attempt states through lifecycle.AttemptStateMachine "
                                        "(e.g. self.life.to_ready(a, ...)), not store.update(..., state=...)")

    def test_the_guard_finds_store_writes(self):
        src = ("self.store.update(a, state='ready', note='x')\n"
               "store.update(a, expect='waiting', state='running')\n"
               "self.store.update_with_marks(a, {}, expect='waiting', state='ready')\n"
               "other.update_with_marks(a, marks, state=s)\n"
               "self._store.update(a, state=s)\n"
               "x.update(a, state='aborted')\n")
        self.assertEqual(state_writes(src), [1, 2, 3, 4, 5, 6])

    def test_the_guard_ignores_unrelated_updates(self):
        src = ("record.update(state='open', request=r)\n"
               "candidate.update(state='waiting', checkout={})\n"
               "journal.update(phase=name, **facts)\n"
               "self.store.update(a, wait=w)\n"
               "self.store.update_with_marks(a, marks, expect='waiting', wait=w)\n"
               "self.life.to_ready(a, marks={}, expect='waiting')\n"
               "self.life.to(a, state, note=n)\n")
        self.assertEqual(state_writes(src), [])


class _RecordingStore:
    def __init__(self, result):
        self.calls, self.result = [], result

    def update(self, a, expect=None, **changes):
        self.calls.append(("update", a, expect, changes))
        return self.result

    def update_with_marks(self, a, marks, expect=None, expect_wait=None, validate=None, **changes):
        self.calls.append(("update_with_marks", a, marks, expect, expect_wait, validate, changes))
        return self.result


class AttemptStateMachinePassesThrough(unittest.TestCase):
    def test_without_marks_it_is_store_update(self):
        store = _RecordingStore(True)
        life = lifecycle.AttemptStateMachine(store)
        self.assertIs(life.to_waiting("a", expect="running", wait={"kind": "time"}, no_outcome=0), True)
        self.assertEqual(store.calls, [("update", "a", "running",
                                        {"state": "waiting", "wait": {"kind": "time"}, "no_outcome": 0})])

    def test_with_marks_it_is_update_with_marks_even_when_empty(self):
        store = _RecordingStore(False)
        life = lifecycle.AttemptStateMachine(store)
        check = lambda: True  # noqa: E731
        self.assertIs(life.to_ready("a", marks={}, expect="waiting", expect_wait={"k": 1}, validate=check,
                                    wake_text="go"), False)
        self.assertEqual(store.calls, [("update_with_marks", "a", {}, "waiting", {"k": 1}, check,
                                        {"state": "ready", "wake_text": "go"})])

    def test_every_state_has_its_method(self):
        names = {lifecycle.STARTING: "to_starting", lifecycle.RUNNING: "to_running", lifecycle.WAITING: "to_waiting",
                 lifecycle.READY: "to_ready", lifecycle.UNKNOWN: "to_unknown", lifecycle.COMPLETED: "to_completed",
                 lifecycle.FAILED: "to_failed", lifecycle.ABORTED: "to_aborted",
                 lifecycle.ABORTED_NOTIFIED: "to_aborted_notified"}
        self.assertEqual(set(names), set(lifecycle.STATES))
        for state, name in names.items():
            store = _RecordingStore(True)
            getattr(lifecycle.AttemptStateMachine(store), name)("a")
            self.assertEqual(store.calls, [("update", "a", None, {"state": state})], name)
            store = _RecordingStore(True)
            lifecycle.AttemptStateMachine(store).to("a", state, marks={"m": "v"}, note="n")
            self.assertEqual(store.calls, [("update_with_marks", "a", {"m": "v"}, None, None, None,
                                            {"state": state, "note": "n"})], state)


if __name__ == "__main__":
    unittest.main()
