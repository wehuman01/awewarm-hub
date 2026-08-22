# Contributing to awewarm-hub

## Engineering Taste

- Simple: make the smallest change that solves the real problem.
- Clear: optimize for the next reader, not for cleverness.
- Decoupled: keep boundaries clean, but do not add abstractions without a real need.
- Honest: make complexity, state, side effects, assumptions, and failure modes visible; do not hide complexity or create extra complexity.
- Focused: preserve boundaries between modules, and keep top-level convenience commands minimal.
- Durable: choose behavior that is easy to maintain, test, and extend.
- First principles: identify the real problem, hard constraints, and known facts before reaching for patterns, abstractions, or prior solutions.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ../awewarm -e .
python3 -m unittest discover -s tests
```

The engine (WarmServer, schedule, transport, the HTTP handler core) comes from the
`awewarm` pip dependency — install it editable from the sibling checkout for
cross-repo work. The only direct dependency this package adds is `click`. Do not
add a dependency unless it clearly earns its cost.

## Stable design constraints

These are facts and rules, not suggestions. Changes here need a spec update
first.

- **The engine is a pinned pip dependency**: `awewarm>=X.Y,<X+1.0` — the minor
  version is locked so the wire protocol stays in lockstep with the open-source
  client. Engine internals are imported from `awewarm`, not copied here;
  `server._Handler` is the semi-public extension surface this package builds
  on. Bump the pin only with an engine release, in the same change.
- **No secret ever touches disk**: API keys live in server RAM only and are
  re-pushed by each user's machine after a restart. `tenants.json` stores
  SHA-256 hashes of tenant tokens (pairings survive restarts); invite codes are
  the one deliberate exception — kept in the clear so the operator can recover
  one already sent, which is exactly why the data dir must be guarded.
- **Tenant isolation**: each tenant gets a private workspace under
  `tenants/<id>/` — connections, state, and the RAM keyring are invisible across
  tenants. Never add a code path that reads across workspaces.
- **Hub registry mutations are cross-process transactions**: the resident
  `serve` process and one-shot CLI processes (invite, revoke, restore, usage
  writes) share the transaction lock from `awewarm.locking` under the server
  data dir. Every mutation refreshes from disk, changes memory, and atomically
  saves while holding that lock; otherwise a stale usage write could revive a
  revoked tenant.
- **Revocation is suspension, not deletion**: `revoke` + `restore` round-trips
  a tenant or an invite; a suspended tenant frees its capacity slot. Nothing
  user-visible is ever destroyed by an admin action.
- **Data-dir compatibility**: the default `~/.awewarm-server` (and any dir set
  by the old `awewarm hub config --data-dir`) must keep working across
  releases and upgrades from the pre-split package.
- **The trust rule is stated plainly, everywhere**: the hub fires requests with
  its users' API keys, so their plaintext keys pass through its RAM. Docs and
  help text must not soften this. Hub for people who trust the machine's
  operator (and root); a shared VPS with strangers is not that.
- **Serve stamps its caps into `tenants.json`** at launch so one-shot CLI
  processes on the same box read the same numbers; a data dir whose serve never
  launched says "caps unknown" instead of guessing.

## Code style

Follow the sibling project aweswitch: module-level functions with explicit
arguments (that is the test seam), plain dicts validated with `isinstance`,
`die()` with actionable multi-line errors, English docstrings that explain why.
Tests are `unittest` + `click.testing.CliRunner`, one file per module, with
env-overridable paths (`AWEWARM_CONFIG` etc., shared with the engine) for
isolation.

When behavior changes, update README.md, README_cn.md, README.ai.md,
docs/CHANGELOG.md, the packaged skill (`resources/skills/awewarm-hub/SKILL.md`),
and the affected tests in the same change.

## Branch model and release

Work lands on `main`, then tagged `vX.Y.Z`. The release workflow
(`.github/workflows/release.yml`) verifies the tag matches the package
version, extracts the matching `## vX.Y.Z` section from `docs/CHANGELOG.md` as
release notes, and publishes to PyPI. Add a CHANGELOG entry as part of any
user-visible change. Release the engine (`awewarm`) before this package — the
dependency pin resolves against PyPI.
