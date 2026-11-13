# Changelog fragments

One file per change: `changelog.d/<card>-<slug>.md`, where `<card>` is the 8-hex id of the card the
merge request delivers and `<slug>` is lowercase words joined by hyphens
(`0123abcd-task-writer-hold.md`); the slug may be left out (`0123abcd.md`). Two merge requests
never write the same file, so they never conflict here.

The file holds the entry exactly as it will read in `CHANGELOG.md`: one or more list items, no
headings. The file name carries the card; the entry text does not repeat it (`mr-policy` refuses a
card id in the text: the changelog is published).

```markdown
- **What changed, in one sentence.** Why it matters, what an operator sees, the new
  option and its default, the doc to read.
```

A merge request that changes `src/` without a fragment is refused by the `mr-policy` CI job. A change
that needs no entry (a pure refactor, a test helper) carries the label `no-changelog` or a
description line `Changelog: none`; re-run the job after adding either.

Do not edit `CHANGELOG.md` or the version: the release commit runs `python3 ops/release.py
<version>`, which writes the section and deletes the fragments. See `docs/release-process.md`.
