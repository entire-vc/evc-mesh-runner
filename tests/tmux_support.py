"""tmux for the tests that drive a real terminal on a private server.

Without tmux these tests are skipped, so a machine without it still runs the rest of the suite.
With MESH_RUNNER_REQUIRE_TMUX=1 (CI sets it) a missing tmux fails every such test instead, so the
interactive driver tests cannot pass as silent skips.

Use `needs_tmux` on a TestCase class or a test method, or `check_tmux(self)` in setUp. The check
runs when the test runs, not at import, so the requirement follows the environment of that run.
"""
import functools
import inspect
import os
import shutil
import unittest

REQUIRE_ENV = "MESH_RUNNER_REQUIRE_TMUX"
SKIP_REASON = "tmux not installed"
TMUX = shutil.which("tmux")


def tmux_required(environ=None):
    value = (os.environ if environ is None else environ).get(REQUIRE_ENV, "")
    return value.strip() not in ("", "0")


def check_tmux(testcase):
    """The tmux binary; otherwise skip the test, or fail it when tmux is required."""
    if TMUX:
        return TMUX
    if tmux_required():
        testcase.fail(f"{SKIP_REASON} but {REQUIRE_ENV}=1: install tmux before the suite runs")
    testcase.skipTest(SKIP_REASON)


def needs_tmux(obj):
    """Decorate a TestCase class (its setUp checks first) or one test method."""
    if inspect.isclass(obj):
        if not issubclass(obj, unittest.TestCase):
            raise TypeError(f"needs_tmux: {obj!r} is not a TestCase")
        original = obj.setUp

        @functools.wraps(original)
        def setUp(self, *args, **kwargs):
            check_tmux(self)
            return original(self, *args, **kwargs)

        obj.setUp = setUp
        return obj

    @functools.wraps(obj)
    def test(self, *args, **kwargs):
        check_tmux(self)
        return obj(self, *args, **kwargs)

    return test
