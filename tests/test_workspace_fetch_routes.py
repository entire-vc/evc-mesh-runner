import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.store import Store
from mesh_runner.config import ConfigError, load
from mesh_runner.workspace import RepoSpec, WorkspaceError, WorkspacePool, git


PUBLIC = "https://github.com/example/brandkit.git"
CANON = "ssh://git@git.example:2222/example/brandkit.git"
PARENT = "ssh://git@git.example:2222/example/contenthub.git"


class FetchRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
                    "GIT_ALLOW_PROTOCOL": "file:ssh:https", "GIT_SSH_VARIANT": "ssh"}
        for name in ("canon", "mirror"):
            (self.root / name).mkdir()
        self.child_remote = self.root / "canon/brandkit.git"
        self.parent_remote = self.root / "canon/contenthub.git"
        for path in (self.child_remote, self.parent_remote):
            self.g(self.root, "init", "-q", "--bare", "-b", "main", str(path))
        self.g(self.root, "clone", "-q", str(self.child_remote), "child")
        self.child = self.root / "child"
        (self.child / "x").write_text("old")
        self.g(self.child, "add", ".")
        self.g(self.child, "commit", "-qm", "old")
        self.g(self.child, "push", "-q", "origin", "main")
        self.g(self.root, "clone", "-q", "--bare", str(self.child_remote), "mirror/brandkit.git")
        self.g(self.root, "clone", "-q", str(self.parent_remote), "author")
        author = self.root / "author"
        self.g(author, "submodule", "add", "-q", str(self.root / "mirror/brandkit.git"), "vendor/brandkit")
        self.g(author, "config", "-f", ".gitmodules", "submodule.vendor/brandkit.url", PUBLIC)
        self.g(author, "add", ".")
        self.g(author, "commit", "-qm", "old parent")
        self.g(author, "push", "-q", "origin", "main")
        # Existing count must survive. The broader public prefix reaches only
        # the old local mirror; the exact override has a longer match.
        self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.{self.root}/mirror/.insteadOf",
                        GIT_CONFIG_VALUE_0="https://github.com/example/")
        self.g(self.root, "clone", "-q", "--recurse-submodules", str(self.parent_remote), "base")
        self.base = self.root / "base"
        self.g(self.base, "remote", "set-url", "origin", PARENT)
        # A local SSH transport exercises real Git recursive child inheritance,
        # with no network, credentials, or mocks of the fetch itself.
        shim = self.root / "ssh.py"
        shim.write_text("import sys,shlex,subprocess,pathlib\n"
                        "cmd=shlex.split(sys.argv[-1])\n"
                        "assert cmd[0]=='git-upload-pack'\n"
                        "name=pathlib.PurePosixPath(cmd[1]).name\n"
                        "assert name in ('brandkit.git','contenthub.git')\n"
                        f"sys.exit(subprocess.call(['git-upload-pack',str(pathlib.Path({str(self.root / 'canon')!r})/name)]))\n")
        self.env["GIT_SSH_COMMAND"] = shlex.join([sys.executable, str(shim)])
        (self.child / "x").write_text("new")
        self.g(self.child, "commit", "-qam", "new")
        self.g(self.child, "push", "-q", "origin", "main")
        self.target = self.g(self.child, "rev-parse", "HEAD")
        self.g(author, "config", "-f", ".gitmodules", "submodule.vendor/brandkit.url", "../brandkit.git")
        self.g(author, "update-index", "--cacheinfo", f"160000,{self.target},vendor/brandkit")
        self.g(author, "add", ".gitmodules")
        self.g(author, "commit", "-qm", "canonical gitlink")
        self.g(author, "push", "-q", "origin", "main")
        self.store = Store(self.root / "state.sqlite")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def g(self, cwd, *args):
        return git(cwd, *args, env=self.env)

    def pool(self, overrides=(), base=None):
        return WorkspacePool(self.store, self.root / "pool",
                             [RepoSpec("app", base or self.base, fetch_url_overrides=overrides)], env=self.env)

    def test_stale_initialized_origin_requires_override_and_recursive_fetch_really_gets_object(self):
        baseline = self.root / "baseline"
        shutil.copytree(self.base, baseline)
        with self.assertRaises(WorkspaceError):
            self.pool(base=baseline).lease("baseline", "app")
        self.assertEqual(self.pool(base=baseline).rows(), [])
        self.assertNotEqual(subprocess.run(["git", "-C", str(baseline / "vendor/brandkit"), "cat-file", "-e",
                                            self.target], capture_output=True).returncode, 0)
        before = (self.base / ".git/config").read_bytes()
        child_before = (self.base / ".git/modules/vendor/brandkit/config").read_bytes()
        old_head = self.g(self.base, "rev-parse", "HEAD")
        saved_env = dict(self.env)
        slot = self.pool(((PUBLIC, CANON),)).lease("fixed", "app")
        self.g(self.base / "vendor/brandkit", "cat-file", "-e", self.target + "^{commit}")
        self.assertEqual(self.g(slot, "rev-parse", "HEAD"), self.g(self.base, "rev-parse", "origin/main"))
        self.assertEqual(self.g(self.base, "rev-parse", "HEAD"), old_head)
        self.assertEqual((self.base / ".git/config").read_bytes(), before)
        self.assertEqual((self.base / ".git/modules/vendor/brandkit/config").read_bytes(), child_before)
        self.assertEqual(self.env, saved_env)

    def test_new_canonical_copy_needs_no_override_and_inherits_existing_git_env(self):
        fresh = self.root / "fresh"
        self.g(self.root, "clone", "-q", "--recurse-submodules", PARENT, str(fresh))
        self.pool(((PUBLIC, CANON),), fresh).lease("new", "app")
        self.g(fresh / "vendor/brandkit", "cat-file", "-e", self.target)

    def test_prefix_collision_fails_before_fetch_and_no_slot_allocated(self):
        self.g(self.base / "vendor/brandkit", "remote", "set-url", "origin", PUBLIC + "-external")
        with self.assertRaisesRegex(WorkspaceError, "prefix collision"):
            self.pool(((PUBLIC, CANON),)).lease("suffix", "app")
        self.assertEqual(self.pool().rows(), [])

    def test_external_fork_is_not_rewritten(self):
        pool = self.pool(((PUBLIC, CANON),))
        calls = []
        def fake(cwd, *args, **kw):
            calls.append((args, kw.get("env")))
            if args[0] == "config": return PARENT
            if args[0] == "submodule": return "remote.origin.url https://github.com/external/brandkit.git"
            return ""
        with patch("mesh_runner.workspace.git", side_effect=fake):
            pool._fetch(pool.repos["app"])
        self.assertEqual(calls[-1][1], self.env)

    def test_parent_remote_is_never_eligible_even_when_child_matches(self):
        for child_matches in (False, True):
            with self.subTest(child_matches=child_matches):
                self.g(self.base, "remote", "set-url", "origin", PUBLIC)
                self.g(self.base / "vendor/brandkit", "remote", "set-url", "origin",
                       PUBLIC if child_matches else PARENT)
                pool = self.pool(((PUBLIC, CANON),))
                before = (self.base / ".git/config").read_bytes()
                saved_env = dict(self.env)
                calls = []

                def observe(cwd, *args, **kw):
                    calls.append((args, kw.get("env")))
                    if args[0] == "fetch":
                        self.fail("a parent-targeting override must never reach fetch")
                    return git(cwd, *args, **kw)

                with patch("mesh_runner.workspace.git", side_effect=observe):
                    with self.assertRaisesRegex(WorkspaceError, "may not rewrite parent remote"):
                        pool._fetch(pool.repos["app"])
                self.assertFalse(any(args[0] == "fetch" for args, _ in calls))
                self.assertEqual((self.base / ".git/config").read_bytes(), before)
                self.assertEqual(self.env, saved_env)
                self.assertEqual(pool.rows(), [])

    def test_parent_prefix_cannot_be_rewritten_by_an_exact_child_rule(self):
        self.g(self.base, "remote", "set-url", "origin", PUBLIC + "-parent.git")
        pool = self.pool(((PUBLIC, CANON),))

        def observe(cwd, *args, **kw):
            if args[0] == "fetch":
                self.fail("a parent prefix collision must never reach fetch")
            return git(cwd, *args, **kw)

        with patch("mesh_runner.workspace.git", side_effect=observe):
            with self.assertRaisesRegex(WorkspaceError, "may not rewrite parent remote"):
                pool._fetch(pool.repos["app"])
        self.assertEqual(pool.rows(), [])

    def test_no_override_preserves_fetch_arguments_and_environment(self):
        pool = self.pool()
        with patch("mesh_runner.workspace.git", return_value="") as run:
            pool._fetch(pool.repos["app"])
        run.assert_called_once_with(self.base, "fetch", "origin", "--prune", "--quiet", env=self.env)

    def test_canonical_host_namespace_project_and_credentials_are_guarded(self):
        for target in (CANON.replace("git.example", "evil.example"), CANON.replace("example/", "external/"),
                       CANON.replace("brandkit.git", "other.git"), CANON.replace("git@", "git:password@")):
            with self.subTest(target=target), self.assertRaises(WorkspaceError):
                self.pool(((PUBLIC, target),)).lease("bad", "app")

    def test_existing_config_entries_are_appended_not_changed(self):
        pool = self.pool(((PUBLIC, CANON),))
        calls = []
        def fake(cwd, *args, **kw):
            calls.append((args, kw.get("env")))
            if args[0] == "config": return PARENT
            if args[0] == "submodule": return "remote.origin.url " + PUBLIC
            return ""
        with patch("mesh_runner.workspace.git", side_effect=fake):
            pool._fetch(pool.repos["app"])
        env = calls[-1][1]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], self.env["GIT_CONFIG_KEY_0"])
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], self.env["GIT_CONFIG_VALUE_0"])
        self.assertEqual(env["GIT_CONFIG_KEY_1"], f"url.{CANON}.insteadOf")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], PUBLIC)


class FetchRouteConfig(unittest.TestCase):
    def test_declarative_routes_are_loaded_and_nonstring_tables_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            key.write_text("test-only-key")
            key.chmod(0o600)
            p = root / "runner.toml"
            text = f'''[runner]
mesh_url = "http://mesh.invalid"
[[agents]]
name = "test"
key_file = "{key}"
profile_dir = "{root}"
[[agents.repos]]
name = "app"
base = "{root}"
fetch_url_overrides = {{ "{PUBLIC}" = "{CANON}" }}
'''
            p.write_text(text)
            self.assertEqual(load(p).agents[0].repos[0].fetch_url_overrides, ((PUBLIC, CANON),))
            for value in ('true', '[]', '{ "url" = true }'):
                p.write_text(text.replace('{ "' + PUBLIC + '" = "' + CANON + '" }', value))
                with self.subTest(value=value), self.assertRaises(ConfigError):
                    load(p)


if __name__ == "__main__":
    unittest.main()
