# Internal submodule fetch routes

An initialized base clone can retain a public submodule origin even after a
new parent commit changes `.gitmodules` to a relative canonical URL. The pool
fetches before checking out that commit. An on-demand recursive fetch can then
ask the public export for an internal-only gitlink SHA and fail with `not our ref`.

For an explicitly approved internal route, configure the affected repository:

```toml
[[agents.repos]]
name = "contenthub"
base = "/operator/contenthub"
fetch_url_overrides = { "https://github.com/org/brandkit.git" = "ssh://git@git.example.com:2222/org/brandkit.git" }
```

The pool uses this allowlist only while fetching, under its normal repository
lock. It appends inherited `GIT_CONFIG_*` entries so recursive child fetches use
the internal transport. Existing entries and the agent's SSH identity remain
unchanged. The normal recursion setting and fetch arguments remain unchanged.
No `.gitmodules`, repository config, base HEAD, or harness environment is edited.

The source URL must exactly match an initialized descendant's remote. A URL
with an additional suffix is refused, because Git's `insteadOf` matches prefixes.
Other forks are left alone. The target must use the parent remote's host, port
and namespace, retaining the source project path. Embedded credentials and
unsupported URLs are refused. Uninitialized new copies with canonical relative
URLs need no override. Unsupported or ambiguous routes fail before allocation.

Before enabling a route, independently verify the exact SHA exists in canon,
is unavailable in the public export, and both the parent and child canonical
remotes are readable using the affected lane's existing identity. This feature
does not repair provider authentication, missing canonical objects, or CAS races.

An operator should back up the scoped runner configuration and enable the route
only for the affected base copies after normal review/deployment. Generated
configuration must preserve the field in its source generator; a hand edit that
will be dropped by regeneration is insufficient. Do not run `submodule sync` on
an active old base: its old `.gitmodules` would restore the public URL.

Rollback removes only this repository's `fetch_url_overrides` entry and reloads
configuration through the normal deployment procedure. There are no persistent
Git settings to undo. Already fetched canonical Git objects remain valid cache.
The underlying stale-origin failure may return until that base is prepared by
its owner; do not disable recursive fetches, delete lockfiles, or reset worktrees.
