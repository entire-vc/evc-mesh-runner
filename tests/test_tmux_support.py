"""MESH_RUNNER_REQUIRE_TMUX: without tmux a terminal test fails instead of skipping."""
import io
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import tmux_support

TESTS = Path(__file__).resolve().parent


def run_case(case):
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return result


def sample_cases():
    calls = []

    @tmux_support.needs_tmux
    class OnClass(unittest.TestCase):
        def setUp(self):
            calls.append("setUp")

        def test_it(self):
            calls.append("class test")

    class OnMethod(unittest.TestCase):
        @tmux_support.needs_tmux
        def test_it(self):
            calls.append("method test")

    class InSetUp(unittest.TestCase):
        def setUp(self):
            self.binary = tmux_support.check_tmux(self)

        def test_it(self):
            calls.append(self.binary)

    return calls, (OnClass, OnMethod, InSetUp)


class RequireTmux(unittest.TestCase):
    def outcome(self, tmux, environ):
        calls, cases = sample_cases()
        with mock.patch.object(tmux_support, "TMUX", tmux), mock.patch.dict(os.environ):
            os.environ.pop(tmux_support.REQUIRE_ENV, None)
            os.environ.update(environ)
            results = [run_case(case) for case in cases]
        return calls, results

    def test_missing_tmux_fails_every_form_when_required(self):
        calls, results = self.outcome(None, {tmux_support.REQUIRE_ENV: "1"})
        for result in results:
            self.assertEqual((result.testsRun, len(result.failures), len(result.skipped)), (1, 1, 0))
            self.assertIn("MESH_RUNNER_REQUIRE_TMUX=1", result.failures[0][1])
        self.assertEqual(calls, [])              # the test body never ran, the original setUp neither

    def test_missing_tmux_skips_every_form_when_not_required(self):
        for environ in ({}, {tmux_support.REQUIRE_ENV: "0"}, {tmux_support.REQUIRE_ENV: ""}):
            with self.subTest(environ=environ):
                calls, results = self.outcome(None, environ)
                for result in results:
                    self.assertEqual((len(result.failures), len(result.errors)), (0, 0))
                    self.assertEqual([r for _, r in result.skipped], [tmux_support.SKIP_REASON])
                self.assertEqual(calls, [])

    def test_present_tmux_runs_every_form_required_or_not(self):
        for environ in ({}, {tmux_support.REQUIRE_ENV: "1"}):
            with self.subTest(environ=environ):
                calls, results = self.outcome("/usr/bin/tmux", environ)
                for result in results:
                    self.assertTrue(result.wasSuccessful())
                    self.assertEqual(result.skipped, [])
                self.assertEqual(calls, ["setUp", "class test", "method test", "/usr/bin/tmux"])

    def test_the_switch_is_read_when_the_test_runs_not_at_import(self):
        _, results = self.outcome(None, {tmux_support.REQUIRE_ENV: "1"})
        self.assertEqual(len(results[0].failures), 1)
        _, results = self.outcome(None, {})
        self.assertEqual(len(results[0].skipped), 1)


class EveryTmuxTestUsesTheHelper(unittest.TestCase):
    """A test that finds tmux by itself would skip quietly again where tmux is missing."""

    BYPASS = re.compile(r"""shutil\.which\(\s*["']tmux["']|skip(Unless|If)\([^)]*TMUX|skipTest\([^)]*tmux""")

    def test_no_test_module_skips_on_tmux_by_itself(self):
        offenders = [f"{path.name}:{n}" for path in sorted(TESTS.glob("*.py"))
                     if path.name not in ("tmux_support.py", Path(__file__).name)
                     for n, line in enumerate(path.read_text().splitlines(), 1)
                     if self.BYPASS.search(line) and "# tmux-skip-fixture" not in line]
        self.assertEqual(offenders, [], "use tests/tmux_support.py (needs_tmux / check_tmux)")

    def test_the_bypass_pattern_catches_the_old_forms(self):
        for line in ('TMUX = shutil.which("tmux")', '@unittest.skipUnless(TMUX, "tmux not installed")',
                     "@unittest.skipUnless(ti.TMUX, 'x')", "self.skipTest('private tmux test requires tmux')"):
            self.assertRegex(line, self.BYPASS)


if __name__ == "__main__":
    unittest.main()
