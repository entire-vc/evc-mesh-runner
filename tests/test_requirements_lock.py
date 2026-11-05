"""requirements.lock must cover the declared dependencies (base + every extra), pinned with hashes."""
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def declared(pyproject_text):
    """Normalised names of every declared requirement: base dependencies plus all extras."""
    project = tomllib.loads(pyproject_text)["project"]
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    return {norm(re.split(r"[\s;<>=!~\[(]", r, maxsplit=1)[0]) for r in reqs}


def parse_lock(text):
    """name -> (pinned version, hash count); entries are 'name==ver [; marker] \\ --hash=...' blocks."""
    out = {}
    for m in re.finditer(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)[^\n]*\\?\n((?:\s+--hash=sha256:[0-9a-f]{64}[ \\]*\n?)*)", text, re.M):
        out[norm(m.group(1))] = (m.group(2), len(re.findall(r"--hash=sha256:", m.group(3))))
    return out


def problems(pyproject_text, lock_text):
    """Empty list = lock covers the declaration. Missing/empty/uncovered/unpinned are all refusals."""
    if lock_text is None:
        return ["dependency_lock_missing"]
    lock = parse_lock(lock_text)
    if not lock:
        return ["dependency_lock_empty"]
    found = []
    for name in sorted(declared(pyproject_text)):
        if name not in lock:
            found.append("dependency_lock_uncovered:" + name)
    for name, (_ver, hashes) in sorted(lock.items()):
        if hashes == 0:
            found.append("dependency_lock_unhashed:" + name)
    return found


class RequirementsLock(unittest.TestCase):
    def setUp(self):
        self.py = (ROOT / "pyproject.toml").read_text()
        self.lock = (ROOT / "requirements.lock").read_text()

    def test_committed_lock_covers_declaration(self):
        self.assertEqual(problems(self.py, self.lock), [])

    def test_receipt_extra_is_kept_and_cryptography_is_locked(self):
        self.assertIn("cryptography", declared(self.py))
        lock = parse_lock(self.lock)
        self.assertIn("cryptography", lock)
        self.assertGreaterEqual(int(lock["cryptography"][0].split(".")[0]), 41)
        self.assertGreater(lock["cryptography"][1], 0)

    def test_transitives_are_locked(self):
        # cryptography pulls cffi (and pycparser through it) on CPython
        self.assertLessEqual({"cffi", "pycparser"}, set(parse_lock(self.lock)))

    def test_missing_lock_is_refused(self):
        self.assertEqual(problems(self.py, None), ["dependency_lock_missing"])

    def test_empty_lock_is_refused(self):
        self.assertEqual(problems(self.py, "# nothing\n"), ["dependency_lock_empty"])

    def test_uncovered_declaration_is_refused(self):
        partial = "\n".join(
            block for block in re.split(r"(?m)^(?=\S+==)", self.lock) if not block.startswith("cryptography=="))
        self.assertIn("dependency_lock_uncovered:cryptography", problems(self.py, partial))

    def test_drift_new_declared_dependency_is_detected(self):
        drifted = self.py.replace('dependencies = []', 'dependencies = ["requests>=2"]')
        self.assertIn("dependency_lock_uncovered:requests", problems(drifted, self.lock))

    def test_unhashed_pin_is_refused(self):
        self.assertIn("dependency_lock_unhashed:cryptography",
                      problems(self.py, self.lock + "\ncryptography==99.0.0\n"))


if __name__ == "__main__":
    unittest.main()
