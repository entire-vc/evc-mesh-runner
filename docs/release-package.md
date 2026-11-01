# Release package contract

The catalog artifact of a release is the source archive of the release commit
(`git archive`), not a Python wheel: `pyproject.toml` packages only `src/`, so a wheel or
`pip install` omits `scripts/` and `docs/`. The runner is installed from the unpacked archive
(`PYTHONPATH=<release>/src`, see README "Updating the runner").

`mesh-extension.json` `version` = `mesh_runner.__version__` = `pyproject.toml` `version`, and the
version is a new, unpublished one: a published version is never replaced.

A release archive must contain, besides the rest of the tree:

| Path | Why |
|---|---|
| `src/mesh_runner/reviewer_merge.py` | the `reviewer-merge` command and the merge-only gate |
| `src/mesh_runner/{cli,config,runner,extension_service}.py` | wiring of that command |
| `scripts/select-reviewer.py` | router adapter; needs an installed fleet router, see `docs/reviewer-merge.md` |
| `scripts/reviewer-canary.py` | read-only canary |
| `docs/reviewer-merge.md` | operator contract and rollback |

`tests/test_release_package.py` checks the version sync and, against `git ls-files`, that every
listed path is tracked (so `git archive` carries it), and that the reviewer files carry no card
ids or host paths. It does not publish anything; a public export is admitted separately.

Dry run (prints the file list and hashes of the required files):

```
git archive --format=tar HEAD | tar -tf - | grep -E 'reviewer_merge|select-reviewer|reviewer-canary|reviewer-merge.md'
git archive --format=tar HEAD | shasum -a 256
```
