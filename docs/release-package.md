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
| `scripts/select-reviewer.py` | router adapter; requires a separately installed compatible router, see `docs/reviewer-merge.md` |
| `scripts/reviewer-canary.py` | read-only canary |
| `docs/reviewer-merge.md` | operator contract and rollback |
| `src/mesh_runner/session_receiver.py`, `docs/session-receiver.md` | the `session-receiver` command and its operator contract |
| `src/mesh_runner/qa_assignment.py`, `src/mesh_runner/qa_assignment_v1/conformance.json`, `docs/host-local-qa-assignment.md` | the default-off `host_local_qa_assignment_v1` capability, its pins (the frozen bytes stay private, loaded from the operator's `bundle_dir`) and operator contract |

`tests/test_release_package.py` checks the version sync and, against `git ls-files`, that every
listed path is tracked (so `git archive` carries it), and that the reviewer files carry no card
ids or host paths. It does not publish anything; a public export is admitted separately.

Dry run (prints the file list and hashes of the required files):

```
git archive --format=tar HEAD | tar -tf - | grep -E 'reviewer_merge|select-reviewer|reviewer-canary|reviewer-merge.md'
git archive --format=tar HEAD | shasum -a 256
```

## Optional receipt dependency (source install)

The core stays stdlib-only (`dependencies = []`). The enrolled reviewer receipt source verifies the
issuer's Ed25519 signature and therefore needs `cryptography` (declared as the `receipt` extra). Without it
`verify_issuer` refuses (`issuer verification unavailable: cryptography required`): fail closed, never admitted.

The release is installed from the unpacked archive with `PYTHONPATH`, so pip does not resolve extras for it.
Before enabling the receipt source, run the preflight under the same interpreter that starts the runner:

```
<runner-python> -c "import cryptography.hazmat.primitives.asymmetric.ed25519"
```

A non-zero exit means the dependency is missing: install it for that interpreter (`<runner-python> -m pip install 'cryptography>=41'`)
or leave the receipt source disabled. Do not install it ad hoc into a running runtime without a rollback anchor.
