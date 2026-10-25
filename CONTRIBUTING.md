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
