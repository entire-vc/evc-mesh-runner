# Repository access from Mesh, host inventory apart

## Two separate things

**Host inventory**: the top-level `[[repositories]]` section of `runner.toml`, not a `[runner]` option.
It lists which base clones exist on this host, each with its canonical project stated explicitly:

```toml
[[repositories]]
name = "evc-mesh"                 # pool name: worktrees/<agent>/<name>/slot-N
provider = "gitlab"               # default gitlab
host = "git.example.com"          # lowercased; scheme, userinfo and default port dropped
project_path = "entire-vc/evc-mesh"   # leading/trailing '/' and '.git' dropped
base = "/srv/repos/evc-mesh"
remote = "origin"
branch = "main"
max_slots = 4
# fetch_url_overrides = { ... }   # as in agents.repos
```

Names and projects are unique. The canonical project is never derived from a git remote, so a
clone that is missing is still known. Neither a clone nor another lane's config grants anything.

**Permission**: `GET /agents/me` -> `repository_access`, read by `refresh_me()`, the first action
of every tick, before `resume_ready`/`start_new`:

| Mesh answer | Lane leases |
|---|---|
| `null` / field absent, never seen an explicit state | legacy `agents.repos` (temporary fallback) |
| `{revision, repositories: [{provider, host, project_path}]}` | inventory entries whose canonical project is listed |
| `{revision, repositories: []}` | nothing: an explicit deny-all |

Matching uses normalized `(provider, host, project_path)` only; the same `project_path` on another
host or provider is a different repository.

## Last known good

- A valid explicit state is stored in the runner's Store (`marks`, task `_repository_access`). A
  restart during a Mesh outage starts from it, not from legacy.
- Mesh unreachable, a malformed answer (whole answer refused, never a partial list), or a later
  `null`/missing field after an explicit state: the last valid state stands. Logged once per
  revision/payload. A damaged stored record denies everything until Mesh answers.
- Mesh raises `revision` on every change, so only a strictly greater revision replaces the current
  state (and the stored record). An older revision, or different content at the same revision, is a
  stale or reordered answer: ignored and logged once, so it can never undo a newer revoke.
- Where Mesh and legacy `agents.repos` differ, it is logged once per revision/composition.

## Admission

- Preflight (`repo_requirements.check(..., access=...)`): not allowed by Mesh -> `no_membership`;
  allowed but no inventory entry or no clone on disk -> `missing_approved_clone`. Both name the
  Mesh / inventory action. The fingerprint carries the source, so a lane moving to Mesh hands the
  new action off once.
- A new revision drops the held-card cache at once (`_repo_hold_checked`), also when the lane's
  local repository names did not change (e.g. `no_membership` -> `missing_approved_clone`).
- Grant: newly allowed inventory clones are registered in the pool hot, and the failed-start backoff an
  access refusal set is dropped; the next tick starts the card. A deadline another
  failure set since stays.
- A Mesh grant is the runner's permission only: the lane still fetches with its own forge identity.
  Give that user at least Reporter on the project before the `PUT`, else the start fails on
  `git fetch` and the whole lane's starts pause for `abort_backoff_minutes`.
- Revoke: a running process is not stopped and its leases are not released; the repository stays
  registered in the pool while the lane holds slots of it. New starts, and resumes whose card needs
  it, are checked against the current permission. Under Mesh access a config entry alone does not
  pass `admit-workspace-repo`.
- `status.json` per lane: `repository_access` = `{source: legacy|mesh, revision, repositories, lane_repos}`.

## Who is told about a refusal

`undeclared_repo`, `no_membership` and `missing_approved_clone` need whoever grants repository
access and creates host clones. That addressee is `[runner] repo_access_contacts`: Mesh agent names
in order of preference, e.g. `repo_access_contacts = ["<infra agent>", "<fallback lead>"]`. The
handoff comment then opens with the line

    @<contact> repository access for lane <lane>: <reason> <group/project>

followed by `[runner] repository preflight: ...; next_actor=<contact>; action: <full action>` and the
`repo_preflight={...}` facts. It does not start with `[runner`, so the contact's runner files a
request card from the mention (`deliver_mentions`). A lane never addresses itself (its own mention
files no card): the contact's own lanes address the next name in the list.

- Once per fingerprint and addressee. The same refusal on later ticks writes nothing; a changed
  fingerprint writes one new signal: another repository or reason, or a new Mesh `revision` of the
  lane's access that still leaves the card blocked.
- A card held by a release without addressing (bare fingerprint mark) is addressed once after the
  upgrade, and once more if the addressee changes.
- Runner-side reasons (`checkpoint_conflict`, `no_assigned_checkout`) keep the plain `[runner]`
  comment with `next_actor=runner`; `no_free_slot` writes nothing. Neither addresses anyone.
- An access reason sorts before a runner-side one, so a checkpoint never hides it.
- `repo_access_contacts` unset or `[]`: the plain `next_actor=operator` comment, as before.

## Install and rollback (parent card)

1. Mesh API with `repository_access` merged and deployed; the Runner reads `null` until then.
2. Add `[[repositories]]` for every base clone of `agents.repos`, keeping the same `name`s (pool
   names and checkpoint journals refer to them).
3. Install the release by the ordinary path; check `status.json` -> `repository_access.source == "legacy"` for every lane.
4. Per lane, write the initial list through `PUT /agents/{id}/repository-access` (operator, or
   runtime_manager for another lane). The next tick shows `source: "mesh"`, the revision and `lane_repos`.
   Each granted project needs the lane's forge user as a member (Reporter or above) first. A lane
   whose `agents.repos` include a clone without a forge origin (a local bare repository) cannot
   move to Mesh access: the inventory has no entry for it and the explicit list would drop it.

Rollback: reinstall the previous release. It ignores `[[repositories]]` and the stored record and
goes back to `agents.repos`. The Mesh API cannot set a lane's list back to `null`, so one lane
cannot return to legacy on its own: to give it its old reach without a rollback, `PUT` the projects
its `agents.repos` named. A full return to legacy is the release rollback above.
