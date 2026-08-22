<div align="center">
  <img src="logo/hero2.webp" alt="awewarm-hub" width="860">
  <h1>awewarm-hub: Multi-tenant Hub Server</h1>
  <p><strong>One always-on box keeps a whole team's subscription windows warm.</strong></p>
  <p>The multi-tenant hub server for <a href="https://github.com/wehuman01/awewarm">awewarm</a>: many users, one server, one-time invites — each user's secrets staying on their own machine.</p>
  <p>
    <strong>English</strong> ·
    <a href="./README_cn.md">简体中文</a>
  </p>
  <p>
    <a href="https://ko-fi.com/mugpeng"><img src="https://img.shields.io/badge/Ko--fi-Buy%20me%20a%20coffee-FF5E5B?style=flat-square&logo=ko-fi&logoColor=white" alt="Ko-fi"></a>
  </p>
  <p>
    <img src="https://img.shields.io/pypi/v/awewarm-hub?style=flat-square&color=7C3AED" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A5%203.9-0EA5E9?style=flat-square" alt="Python">
    <img src="https://img.shields.io/badge/license-MPL--2.0-22C55E?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip install">
    <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-334155?style=flat-square" alt="Platform">
    <img src="https://img.shields.io/pepy/dt/awewarm-hub?style=flat-square" alt="PyPI downloads">
    <img src="https://img.shields.io/github/stars/wehuman01/awewarm-hub?style=flat-square" alt="GitHub stars">
  </p>
</div>

> Real case: a five-person team on one $5 VPS. Everyone's subscription plans stay warm around the clock on the box that never sleeps — while every API key stays on each member's own laptop.

awewarm-hub splits the roles in two:

- **Operator** (this machine, the 24/7 box): `awewarm-hub serve` + the admin commands below. You mint one-time invite codes and hand them out.
- **Users** (their own machines, plain open-source [awewarm](https://github.com/wehuman01/awewarm) only): `awewarm remote connect <url> --invite awi_...`, then `awewarm config set <id> --remote`. They never need this package.

## Install

Requires Python ≥ 3.9:

```bash
pip install awewarm-hub          # brings awewarm with it
```

## Quick Start

### Let an AI agent set it up

Working in Claude Code, Codex, or another coding agent? Tell it:

```text
Read https://github.com/wehuman01/awewarm-hub/blob/main/README.ai.md and follow it to set up your awewarm-hub server.
```

The agent installs the CLI, checks status and tenants read-only, and mints invite codes on your request. The resident `serve` process itself stays in your terminal (or systemd) — an agent never backgrounds it.

### Manual setup

```bash
awewarm-hub serve                # listens on 127.0.0.1:8790, data at ~/.awewarm-server
awewarm-hub invite --note alice  # prints awi_... (one use, 48 h)
awewarm-hub status               # capacity, invite counts, tenants, serve liveness
```

## awewarm and awewarm-hub

Two packages, two roles, same [MPL-2.0](LICENSE) license and the same [wehuman01](https://github.com/wehuman01) org:

| Package | Who installs it | What it does |
| --- | --- | --- |
| [awewarm](https://github.com/wehuman01/awewarm) | everyone (each user's own machine) | schedules warm-up requests; `awewarm serve` covers one solo server |
| **awewarm-hub** (this package) | the operator only (the 24/7 box) | multi-tenant serving: tenants, one-time invites, quotas, revoke/restore |

The engine underneath (WarmServer, schedule, transport, the HTTP handler core) comes from the `awewarm` pip dependency, pinned to its minor version so the wire protocol stays in lockstep with the open-source client. Expose the hub through a cloudflared tunnel (free TLS, no open inbound ports, your origin IP stays hidden) exactly like a solo awewarm server — see awewarm's README → *Remote Server*. A systemd user unit is the same shape, with `ExecStart=awewarm-hub serve`:

```ini
[Unit]
Description=awewarm-hub serve
After=network-online.target

[Service]
ExecStart=awewarm-hub serve
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now awewarm-hub` (with `loginctl enable-linger $USER` on headless boxes).

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

## How It Works

Each tenant gets a private workspace under `tenants/<id>/` (connections, state, RAM keyring — invisible across tenants). `tenants.json` stores SHA-256 hashes of tenant tokens, so pairings survive a restart; invite codes are kept in the clear so the operator can recover one already sent (`list invites --reveal`) — anyone who can read the data dir can use a pending invite, so guard it. Revocation is suspension, not deletion: `revoke` + `restore` round-trips a tenant or an invite; a suspended tenant frees its capacity slot. A token serves one machine by default (`--max-machines`); `revoke` + `restore` clears its paired machines. A light per-tenant rate limit (60 requests/minute) stops a looping client.

One trust rule, stated plainly: the hub fires requests with its users' API keys, so their plaintext keys pass through its RAM. Hub for people who trust the machine's operator (and root); a shared VPS with strangers is not that.

Nothing secret is ever written to disk — API keys live in server RAM and are re-pushed by each user's machine after a restart.

## Upgrading from pre-split `awewarm serve --hub`

The data dir (`~/.awewarm-server`, or whatever `--data-dir`/the old `hub config --data-dir` set) carries over unchanged — tenants, invites, and the persisted data-dir setting all keep working. Stop the old serve, install this package, start `awewarm-hub serve`. The old spellings (`awewarm serve --hub`, `awewarm hub ...`) die with a tombstone in awewarm naming their replacement here.

## Config

`awewarm-hub config [--data-dir /data]` persists the default data dir on the hub machine (the flag overrides once; `--unset` clears it). The default is `~/.awewarm-server`, shared with awewarm's solo server. Inside it: `tenants.json` (token hashes, invite codes, the serve record with caps/bind/version/start time) and one private `tenants/<id>/` workspace per tenant. Serve stamps its caps into `tenants.json` at launch so one-shot CLI processes on the same box read the same numbers; a data dir whose serve never launched says "caps unknown" instead of guessing.

## Self-Update

```bash
awewarm-hub self-update            # upgrade to the latest release
awewarm-hub self-update --check    # show versions only
```

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ../awewarm -e .     # the open-source engine, editable, plus this package
python3 -m unittest discover -s tests
```

`awewarm-hub -v` says `editable` (with the git state) when running from this checkout; pip's recorded metadata freezes at `pip install -e .` time, so re-run it after a version bump to keep `pip show` in sync. `awewarm-hub self-update` refuses on a checkout — pull and re-install instead.

The source repository is [wehuman01/awewarm-hub](https://github.com/wehuman01/awewarm-hub) (open, MPL-2.0); releases are built and published to PyPI on tag push. See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for the engineering doctrine and [docs/CHANGELOG.md](docs/CHANGELOG.md) for release history.

## Support

If awewarm-hub keeps your team's quotas warm, consider supporting it:

- ⭐ Star the repo — it helps others find it.
- ☕ [Ko-fi](https://ko-fi.com/mugpeng) — buy me a coffee.
- 💬 WeChat — scan the QR code below.

<p align="center">
  <img src="assets/images/wechat-pay.jpg" alt="WeChat Pay" width="240">
</p>

> awewarm-hub is free and open source. Sponsors keep it maintained — thank you.
