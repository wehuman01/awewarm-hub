# awewarm-hub

Multi-tenant hub server for [awewarm](https://github.com/wehuman01/awewarm): one always-on box keeps many users' AI coding-plan subscription windows warm, each user's secrets staying on their own machine. Open-source under [MPL-2.0](LICENSE) — the same license as awewarm — and published beside it under [wehuman01](https://github.com/wehuman01); this package depends on the `awewarm` engine as a pip package, pinned to its minor version.

- **Operator** (this machine, the 24/7 box): `awewarm-hub serve` + the admin commands below.
- **Users** (their own machines, open-source awewarm only): `awewarm remote connect <url> --invite awi_...`, then `awewarm config set <id> --remote`. They never need this package.

## Setup

```bash
pip install awewarm-hub          # brings awewarm with it
awewarm-hub serve                # listens on 127.0.0.1:8790, data at ~/.awewarm-server
awewarm-hub invite --note alice  # prints awi_... (one use, 48 h)
```

Expose it through a cloudflared tunnel (free TLS, no open inbound ports) exactly like a solo awewarm server — see awewarm's README → *Remote server*. A systemd unit is the same shape, with `ExecStart=awewarm-hub serve`.

Upgrading from the pre-split `awewarm serve --hub`: the data dir (`~/.awewarm-server`, or whatever `--data-dir`/the old `hub config --data-dir` set) carries over unchanged — tenants, invites, and the persisted data-dir setting all keep working. Stop the old serve, install this package, start `awewarm-hub serve`.

## Commands

```bash
awewarm-hub serve [--data-dir/--bind/--port]   # the resident hub server
                 [--max-tenants/--max-conns-per-tenant/--max-machines/--tick-seconds]
awewarm-hub status [--details]                 # capacity, invite counts, tenants, serve liveness
awewarm-hub invite [--note <who>] [--expires-hours N]
awewarm-hub list users [--api|--reveal|--json] # tenants: health, usage, machines, joining code
awewarm-hub list invites [--reveal|--json]     # every minted code: pending/used/revoked/expired
awewarm-hub revoke <t_...>|<awi_...>           # suspend a tenant / kill an invite (reversible)
awewarm-hub restore <t_...>|<awi_...>          # undo a revoke
awewarm-hub config [--data-dir /data|--unset]  # default data dir for this machine
awewarm-hub self-update [--check]              # upgrade from PyPI
```

## How it works

Each tenant gets a private workspace under `tenants/<id>/` (connections, state, RAM keyring — invisible across tenants). `tenants.json` stores SHA-256 hashes of tenant tokens, so pairings survive a restart; invite codes are kept in the clear so the operator can recover one already sent (`list invites --reveal`) — anyone who can read the data dir can use a pending invite, so guard it. Revocation is suspension, not deletion: `revoke` + `restore` round-trips a tenant or an invite; a suspended tenant frees its capacity slot. A token serves one machine by default (`--max-machines`); `revoke` + `restore` clears its paired machines. A light per-tenant rate limit (60 requests/minute) stops a looping client.

One trust rule, stated plainly: the hub fires requests with its users' API keys, so their plaintext keys pass through its RAM. Hub for people who trust the machine's operator (and root); a shared VPS with strangers is not that.

Nothing secret is ever written to disk — API keys live in server RAM and are re-pushed by each user's machine after a restart.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../awewarm -e . pytest   # the open-source engine, editable, plus this package
python -m pytest
```

The source repository is [wehuman01/awewarm-hub](https://github.com/wehuman01/awewarm-hub) (open, MPL-2.0); releases are built and published to PyPI via Trusted Publishing on tag push.
