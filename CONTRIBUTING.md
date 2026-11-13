# Contributing

- English in code, comments, commit messages and docs.
- Standard library only; Python ≥ 3.11.
- Every behaviour change comes with a test that fails without it:
  `PYTHONPATH=src:tests python3 -m unittest discover -s tests`.
- Never log or print secrets (agent keys, tokens). Configuration references secrets by
  file path or environment variable name.
- Keep the runner policy-free: team-specific rules belong in an agent's profile or a
  preamble file, not in this code.
- Conventional commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Changelog: one file per change in `changelog.d/<card>-<slug>.md` (`changelog.d/README.md`).
  Never edit `CHANGELOG.md` or the version outside the release commit (`docs/release-process.md`);
  the `mr-policy` CI job refuses it.
- No internal card ids (`#` and 8 hex) or team member names in code, comments, docs or changelog
  text: this repository is published. The card goes into the fragment's file name, the branch and
  the merge request; a test fixture uses a stub id (`STUB_CARD_IDS` in `ops/check_mr_policy.py`).
  `mr-policy` and `tests/test_release_flow.py` refuse it.
