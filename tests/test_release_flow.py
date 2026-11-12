"""Changelog fragments, the release script and the merge request gate (ops/release.py, ops/check_mr_policy.py)."""
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


release = load("ops_release", "ops/release.py")
policy = load("ops_check_mr_policy", "ops/check_mr_policy.py")

CHANGELOG = """---
created: 2026-11-01T03:45:49+03:00
updated: 2026-11-11T17:30:00+03:00
status: review
---

# Changelog

## Unreleased

- **Legacy entry written before fragments.** Still collected.

## 0.2.25 — 2026-11-11

- **Older release.** Untouched.
"""

FRAG_A = "- **First change (#aaaaaaaa).** Line one\n  continues here.\n"
FRAG_B = "- **Second change (#bbbbbbbb).** One line.\n"

EXPECTED = """---
created: 2026-11-01T03:45:49+03:00
updated: 2026-11-12T10:00:00+00:00
status: review
---

# Changelog

## Unreleased

Entries wait in `changelog.d/` until the release commit (`docs/release-process.md`).

## 0.2.26 — 2026-11-12

- **First change (#aaaaaaaa).** Line one
  continues here.
- **Second change (#bbbbbbbb).** One line.
- **Legacy entry written before fragments.** Still collected.

## 0.2.25 — 2026-11-11

- **Older release.** Untouched.
"""


def write_tree(root: Path, version="0.2.25", changelog=CHANGELOG):
    (root / "src/mesh_runner").mkdir(parents=True, exist_ok=True)
    (root / "changelog.d").mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (root / "src/mesh_runner/__init__.py").write_text(f'"""x"""\n__version__ = "{version}"\n')
    (root / "src/mesh_runner/core.py").write_text("VALUE = 1\n")
    (root / "mesh-extension.json").write_text(
        f'{{\n  "schema": 1,\n  "version": "{version}",\n  "kind": "plugin"\n}}\n')
    (root / "CHANGELOG.md").write_text(changelog)
    (root / "changelog.d/README.md").write_text("How to write a fragment.\n")


class Repo:
    """A throwaway git repository with the release-relevant files on main."""

    def __init__(self, tmp: Path):
        self.root = tmp
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        write_tree(tmp)
        self.commit("base")

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=check)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def branch(self, name, start="main"):
        self.git("switch", "-q", "-c", name, start)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def check(self, branch, labels=(), description=""):
        return policy.check(self.root, "main", "HEAD", branch, list(labels), description)


class ReleaseScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_tree(self.root)
        (self.root / "changelog.d/bbbbbbbb-second.md").write_text(FRAG_B)
        (self.root / "changelog.d/aaaaaaaa-first-change.md").write_text(FRAG_A)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collects_fragments_into_one_section_and_removes_them(self):   # AC3
        plan = release.plan_release(self.root, "0.2.26", "2026-11-12", "2026-11-12T10:00:00+00:00")
        release.apply(self.root, plan)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(), EXPECTED)
        self.assertEqual(sorted(p.name for p in (self.root / "changelog.d").iterdir()), ["README.md"])
        self.assertIn('version = "0.2.26"', (self.root / "pyproject.toml").read_text())
        self.assertIn('__version__ = "0.2.26"', (self.root / "src/mesh_runner/__init__.py").read_text())
        self.assertIn('"version": "0.2.26",', (self.root / "mesh-extension.json").read_text())
        self.assertFalse(list(self.root.rglob("*.release-tmp")))

    def test_cli_writes_the_same_text(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = release.main(["0.2.26", "--date", "2026-11-12", "--root", str(self.root)])
        self.assertEqual(rc, 0, err.getvalue())
        text = (self.root / "CHANGELOG.md").read_text()
        self.assertRegex(text, r"(?m)^updated: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        body = lambda t: t.split("\n---\n", 1)[1]
        self.assertEqual(body(text), body(EXPECTED))
        self.assertIn("## 0.2.26 — 2026-11-12", out.getvalue())

    def test_a_second_release_keeps_the_note_and_takes_only_new_fragments(self):
        release.apply(self.root, release.plan_release(self.root, "0.2.26", "2026-11-12", "u1"))
        (self.root / "changelog.d/cccccccc-third.md").write_text("- **Third.** x\n")
        release.apply(self.root, release.plan_release(self.root, "0.2.27", "2026-11-13", "u2"))
        text = (self.root / "CHANGELOG.md").read_text()
        self.assertIn("## Unreleased\n\nEntries wait in `changelog.d/` until the release commit "
                      "(`docs/release-process.md`).\n\n## 0.2.27 — 2026-11-13\n\n- **Third.** x\n\n## 0.2.26", text)
        self.assertEqual(text.count("Entries wait in"), 1)

    def test_dry_run_changes_nothing(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(release.main(["0.2.26", "--root", str(self.root), "--dry-run"]), 0)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)

    def assert_refused(self, *argv, match):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = release.main([*argv, "--root", str(self.root), "--date", "2026-11-12"])
        self.assertEqual(rc, 2)
        self.assertIn(match, err.getvalue())
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)

    def test_refusals_write_nothing(self):
        self.assert_refused("0.2.25", match="not newer than the current 0.2.25")
        self.assert_refused("0.3", match="not MAJOR.MINOR.PATCH")
        (self.root / "changelog.d/Bad Name.md").write_text(FRAG_B)
        self.assert_refused("0.2.26", match="name must be <8-hex card>.md or <8-hex card>-<slug>.md")
        (self.root / "changelog.d/Bad Name.md").unlink()
        (self.root / "changelog.d/cccccccc-heading.md").write_text("## 0.9.9\n- x\n")
        self.assert_refused("0.2.26", match="must start with a list item")

    def test_a_directory_in_changelog_d_is_refused_not_a_crash(self):
        (self.root / "changelog.d/cccccccc-dir.md").mkdir()
        self.assert_refused("0.2.26", match="changelog.d/cccccccc-dir.md: not a file")

    def test_refuses_an_existing_section_and_an_empty_release(self):
        with self.assertRaisesRegex(release.ReleaseError, "already has a section for 0.2.25"):
            release.render_changelog(CHANGELOG, "0.2.25", "d", ["- x"], "u")
        with self.assertRaisesRegex(release.ReleaseError, "nothing to release"):
            release.render_changelog(CHANGELOG.replace("- **Legacy entry written before fragments.** "
                                                       "Still collected.\n", ""), "0.2.26", "d", [], "u")


class MergeRequestGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def code_change(self, card, value):
        self.repo.write("src/mesh_runner/core.py", f"VALUE = {value}\n")
        self.repo.write(f"src/mesh_runner/feature_{card}.py", "X = 1\n")
        self.repo.write(f"changelog.d/{card}-feature.md", f"- **Feature {card}.** Text.\n")

    def test_code_with_a_fragment_passes(self):                       # positive control
        self.repo.branch("feat/aaaaaaaa-x")
        self.repo.write("src/mesh_runner/feature.py", "X = 1\n")
        self.repo.write("changelog.d/aaaaaaaa-feature.md", FRAG_A)
        self.repo.commit("feat")
        self.assertEqual(self.repo.check("feat/aaaaaaaa-x"), [])

    def test_code_without_a_fragment_is_refused(self):                 # AC2 negative control
        self.repo.branch("feat/x")
        self.repo.write("src/mesh_runner/feature.py", "X = 1\n")
        self.repo.commit("feat")
        problems = self.repo.check("feat/x")
        self.assertEqual(len(problems), 1)
        self.assertIn("src/ changes without a changelog entry", problems[0])
        self.assertIn("changelog.d/<card>-<slug>.md", problems[0])
        # the opt-out: a label or a description line
        self.assertEqual(self.repo.check("feat/x", labels=["no-changelog"]), [])
        self.assertEqual(self.repo.check("feat/x", description="Refactor.\n\nChangelog: none\n"), [])
        self.assertTrue(self.repo.check("feat/x", description="no changelog: none of that"))

    def test_docs_and_tests_need_no_fragment(self):
        self.repo.branch("docs/x")
        self.repo.write("docs/a.md", "x\n")
        self.repo.write("tests/test_a.py", "x\n")
        self.repo.commit("docs")
        self.assertEqual(self.repo.check("docs/x"), [])

    def test_version_change_outside_release_is_refused(self):          # AC2 negative control
        self.repo.branch("fix/x")
        write_tree(self.repo.root, version="0.2.26", changelog=CHANGELOG)
        self.repo.write("changelog.d/aaaaaaaa-feature.md", FRAG_A)
        self.repo.commit("bump")
        problems = self.repo.check("fix/x")
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(all("version 0.2.25 -> 0.2.26 outside a release/* branch" in p for p in problems))
        self.assertTrue(all("leave it at 0.2.25" in p for p in problems))

    def test_a_single_version_file_is_enough_to_refuse(self):
        self.repo.branch("fix/x")
        self.repo.write("mesh-extension.json", '{\n  "schema": 1,\n  "version": "0.2.26",\n  "kind": "plugin"\n}\n')
        self.repo.commit("bump ext")
        self.assertEqual(len(self.repo.check("fix/x")), 1)

    def test_other_pyproject_edits_pass(self):
        self.repo.branch("chore/x")
        self.repo.write("pyproject.toml", '[project]\nname = "x"\nversion = "0.2.25"\ndependencies = []\n')
        self.repo.commit("deps")
        self.assertEqual(self.repo.check("chore/x"), [])

    def test_changelog_edit_outside_release_is_refused(self):
        self.repo.branch("fix/x")
        self.repo.write("CHANGELOG.md", CHANGELOG.replace("## Unreleased\n", "## Unreleased\n\n- **Mine.** x\n"))
        self.repo.commit("changelog")
        problems = self.repo.check("fix/x")
        self.assertEqual(len(problems), 1)
        self.assertIn("CHANGELOG.md is edited outside a release/* branch", problems[0])

    def test_a_fragment_named_by_the_card_alone_passes(self):
        self.repo.branch("fix/x")
        self.repo.write("src/mesh_runner/feature.py", "X = 1\n")
        self.repo.write("changelog.d/cccccccc.md", FRAG_B)
        self.repo.commit("feat")
        self.assertEqual(self.repo.check("fix/x"), [])
        for bad in ("cccccccc-.md", "CCCCCCCC.md", "ccccccc.md", "cccccccc_x.md"):
            self.assertIsNotNone(release.fragment_problem(bad, FRAG_B), bad)

    def test_malformed_fragment_is_refused(self):
        self.repo.branch("fix/x")
        self.repo.write("changelog.d/My Change.md", "text without a list item\n")
        self.repo.commit("frag")
        self.assertIn("name must be", " ".join(self.repo.check("fix/x")))

    def test_release_branch_made_by_the_script_passes(self):
        self.repo.branch("feat/a")
        self.code_change("aaaaaaaa", 2)
        self.repo.commit("feat a")
        self.repo.git("switch", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "feat/a", "-m", "merge a")
        self.repo.branch("release/0.2.26")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(release.main(["0.2.26", "--root", str(self.repo.root), "--date", "2026-11-12"]), 0)
        self.repo.commit("release: 0.2.26")
        self.assertEqual(self.repo.check("release/0.2.26"), [])
        # the same tree on an ordinary branch is refused
        self.assertTrue(self.repo.check("feat/not-a-release"))

    def test_release_train_merging_a_reviewed_head_passes(self):
        self.repo.branch("feat/b")
        self.code_change("bbbbbbbb", 3)
        self.repo.commit("feat b")
        self.repo.branch("release/0.2.26", "main")
        self.repo.git("merge", "-q", "--no-ff", "feat/b", "-m", "merge b at its reviewed head")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(release.main(["0.2.26", "--root", str(self.repo.root), "--date", "2026-11-12"]), 0)
        self.repo.commit("release: 0.2.26")
        self.assertEqual(self.repo.check("release/0.2.26"), [])

    def test_code_committed_on_a_release_branch_is_refused(self):    # no fragment-free code via release/*
        self.repo.branch("release/0.2.26")
        self.repo.write("src/mesh_runner/hotfix.py", "X = 1\n")
        self.repo.commit("fix on the release branch")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.repo.write("changelog.d/aaaaaaaa-x.md", "- **X.** y\n")
            self.assertEqual(release.main(["0.2.26", "--root", str(self.repo.root), "--date", "2026-11-12"]), 0)
        self.repo.commit("release: 0.2.26")
        problems = self.repo.check("release/0.2.26")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("changes src/mesh_runner/hotfix.py directly", problems[0])

    def test_release_commit_touching_more_than_the_version_line_is_refused(self):
        self.repo.branch("release/0.2.26")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.repo.write("changelog.d/aaaaaaaa-x.md", "- **X.** y\n")
            self.assertEqual(release.main(["0.2.26", "--root", str(self.repo.root), "--date", "2026-11-12"]), 0)
        init = self.repo.root / "src/mesh_runner/__init__.py"
        init.write_text(init.read_text() + "SNEAKY = 1\n")
        self.repo.commit("release: 0.2.26")
        self.assertIn("beyond the version line", " ".join(self.repo.check("release/0.2.26")))

    def test_release_section_without_entries_is_refused(self):
        self.repo.branch("release/0.2.26")
        write_tree(self.repo.root, version="0.2.26",
                   changelog=CHANGELOG.replace("## 0.2.25", "## 0.2.26 — 2026-11-12\n\n## 0.2.25", 1))
        self.repo.commit("release: 0.2.26 by hand")
        self.assertIn("section of CHANGELOG.md has no entry", " ".join(self.repo.check("release/0.2.26")))

    def test_release_branch_without_the_release_commit_is_refused(self):
        self.repo.branch("release/0.2.26")
        self.code_change("aaaaaaaa", 2)
        self.repo.commit("train")
        problems = " ".join(self.repo.check("release/0.2.26"))
        self.assertIn("not newer than main's 0.2.25", problems)
        self.assertIn("fragments not collected", problems)

    def test_release_branch_with_disagreeing_versions_is_refused(self):
        self.repo.branch("release/0.2.26")
        self.repo.write("pyproject.toml", '[project]\nname = "x"\nversion = "0.2.26"\n')
        self.repo.commit("half bump")
        self.assertIn("version files disagree", " ".join(self.repo.check("release/0.2.26")))

    def test_cli_exit_codes(self):
        self.repo.branch("feat/x")
        self.repo.write("src/mesh_runner/feature.py", "X = 1\n")
        self.repo.commit("feat")
        common = ["--root", str(self.repo.root), "--base", "main", "--head", "HEAD", "--labels", "", "--description", ""]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(policy.main([*common, "--branch", "feat/x"]), 1)
            self.assertEqual(policy.main([*common, "--branch", "feat/x", "--labels", "x,no-changelog"]), 0)
            self.assertEqual(policy.main(["--root", str(self.repo.root), "--base", "nope", "--branch", "feat/x"]), 2)


class SequentialMerges(unittest.TestCase):
    """AC1: two merge requests with entries merge one after the other without rebasing the second."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def two_branches(self, entry):
        for card, name in (("aaaaaaaa", "feat/a"), ("bbbbbbbb", "feat/b")):
            self.repo.branch(name, "main")
            self.repo.write(f"src/mesh_runner/feature_{card}.py", "X = 1\n")
            entry(card)
            self.repo.commit(f"feat {card}")
        self.repo.git("switch", "-q", "main")

    def merge(self, name):
        return self.repo.git("merge", "--no-ff", "-m", f"merge {name}", name, check=False)

    def test_fragments_merge_without_conflict(self):
        self.two_branches(lambda c: self.repo.write(f"changelog.d/{c}-feature.md", f"- **{c}.** x\n"))
        self.assertEqual(self.repo.check("feat/b"), [])            # gate on B against the old main
        self.assertEqual(self.merge("feat/a").returncode, 0)
        self.repo.git("switch", "-q", "feat/b")
        self.assertEqual(self.repo.check("feat/b"), [])            # gate on B, unchanged, against the new main
        self.repo.git("switch", "-q", "main")
        res = self.merge("feat/b")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        names = sorted(p.name for p in (self.repo.root / "changelog.d").iterdir())
        self.assertEqual(names, ["README.md", "aaaaaaaa-feature.md", "bbbbbbbb-feature.md"])

    def test_red_control_shared_changelog_lines_conflict(self):
        def edit(card):
            self.repo.write("CHANGELOG.md", CHANGELOG.replace("## Unreleased\n\n", f"## Unreleased\n\n- **{card}.** x\n"))
        self.two_branches(edit)
        self.assertEqual(self.merge("feat/a").returncode, 0)
        res = self.merge("feat/b")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("CONFLICT", res.stdout)
        self.repo.git("merge", "--abort")


class RepositoryLayout(unittest.TestCase):
    def test_fragments_in_this_checkout_are_valid(self):
        release.collect_fragments(ROOT)                            # raises on a malformed fragment

    @unittest.skipUnless((ROOT / ".gitlab-ci.yml").is_file(), "the CI configuration is not part of this tree")
    def test_ci_runs_the_gate_on_merge_requests(self):
        ci = (ROOT / ".gitlab-ci.yml").read_text()
        self.assertIn("mr-policy:", ci)
        self.assertIn("python3 ops/check_mr_policy.py", ci)


if __name__ == "__main__":
    unittest.main()
