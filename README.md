<div align="center">
  <img src="logo/hero2.webp" alt="awewarm-hub" width="860">
  <h1>awewarm-hub: Multi-tenant Hub Server <a href="https://github.com/Webioinfo01/aweskill"><img src="https://raw.githubusercontent.com/Webioinfo01/aweskill/main/logo/aweskill-badge2.svg" alt="aweskill companion"></a></h1>
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
awewarm-hub invite --name alice  # prints awi_... (one use, 7 d)
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
awewarm-hub invite [--name <who>] [--count N] [--expires-in 30m|12h|7d] [--machines N]  # mint (bare form)
awewarm-hub list users [--api|--reveal|--json] # tenants: health, usage, machines, joining code
awewarm-hub list invites [--reveal|--token|--json]  # every minted code: pending/used/revoked/expired, its machine cap, its tenant's token (--token)
awewarm-hub invite rename <id|code> <name>     # relabel an invite's note; a used one's tenant follows
awewarm-hub invite revoke <id|code>            # kill an invite: pending stops pairing, used suspends its tenant (reversible)
awewarm-hub invite revoke <id|code> --delete   # wipe the ledger row outright; a used one takes its tenant (irreversible)
awewarm-hub invite restore <id|code>           # undo a revoke
awewarm-hub invite extend <id|code> --expires-in 7d  # push an expiry out from now; an expired code pairs again
awewarm-hub config [--data-dir /data|--unset]  # default data dir for this machine
awewarm-hub config --max-tenants 20 [--max-conns-per-tenant N] [--max-machines N] [--reset]
                                               # capacity caps; a running serve adopts them without a restart
awewarm-hub config --persist-keys on|off       # may tenants store API keys on this box?
                                               #   off by default; off purges every stored key at once
awewarm-hub self-update [--check]              # upgrade from PyPI
```

The four `invite` subcommands take the invite's id (the ID column `list invites` prints — unique by construction), the full code, or the masked form (`awi_F3XW…`) when it identifies one invite; an ambiguous prefix errors with the candidates. Pairing still demands the full code, so neither an id nor a prefix is a secret, and day-to-day operations never need the full code.

## How It Works

Each tenant gets a private workspace under `tenants/<id>/` (connections, state, RAM keyring — invisible across tenants). `tenants.json` keeps invite codes and tenant tokens in the clear so the operator can recover either one already sent (`list invites --reveal` / `--token`); authentication compares a token's SHA-256 hash, so pairings survive a restart. A user who lost their token is handed it back and reconnects with `awewarm remote connect <url> --token <it>` — same tenant, same connections. Anyone who can read the data dir can spend a pending invite or act as a tenant, so guard it. The invite code is the one ledger of authorization: `revoke awi_...` kills a pending code or suspends the tenant that used it (token rejected, connections stop ticking, capacity slot freed), and `restore awi_...` undoes either — machine pairings are untouched, so the round-trip is lossless. `revoke awi_... --delete` wipes the row from the ledger instead: no revoked tombstone stays, a used one takes its tenant with it (token dead, capacity slot freed, workspace kept on disk), and `restore` cannot bring any of it back; it also accepts an already-revoked row — the purge-a-tombstone case. A machine cap is stamped into each invite at minting (`invite --machines N`, defaulting to the `max-machines` cap); to give an online user more machines, raise the `machines` value on their invite row in `tenants.json` (a running serve adopts the edit) or hand them a fresh code. The three capacity caps live in the registry's serve record: serve flags stamp them at launch, and `awewarm-hub config --max-tenants 20` retunes them at any time — a running serve adopts the new values without a restart (every tenant action and each tick re-read the record). A light per-tenant rate limit (60 requests/minute) stops a looping client.

One trust rule, stated plainly: the hub fires requests with its users' API keys, so their plaintext keys pass through its RAM. Hub for people who trust the machine's operator (and root); a shared VPS with strangers is not that.

API keys never touch disk **by default** — they live in server RAM and are re-pushed by each user's machine after a restart. Invite codes and tenant tokens are the on-disk exceptions above, kept recoverable on purpose; guard the data dir.

One more opt-in exception exists for users whose machines are rarely online: with `awewarm-hub config --persist-keys on` (default off, warns when enabled) a user may confirm per connection (`awewarm config set <id> --persist-key on`) that their key is stored in their workspace's `keys.json` (plaintext, 0600), so a hub restart keeps their warm-ups ticking with no re-push. Both sides can withdraw at any moment: the user turns the flag off (the hub deletes the key), the operator flips the switch off (every stored key on the box is purged at once), and revoking or deleting an invite purges that tenant's keys — the workspace otherwise stays, as always. The feature is discouraged by design; leaving everything off is the recommended posture.

## Security

Short version for users: every ordinary leakage path is closed by design; the one thing that remains is trusting the hub's machine.

- **Account logins never leave your machine.** The hub only accepts API-key (subscription) connections; CLI-account (OAuth) credentials are rejected at the wire and live on wherever you logged in. There is no username, password, or session cookie to leak.
- **Your API key exists on the hub in RAM only** (unless you explicitly opt into `--persist-key` — discouraged; then it sits in your workspace's `keys.json` in plaintext, 0600, and disappears again the moment either you or the operator withdraws). Never logged (activation results are scrubbed of keys and auth headers), and no endpoint reads one back — `/v1/state` reports only whether a key is present. Traffic rides the operator's HTTPS tunnel end to end.
- **A stolen tenant token is not a stolen API key.** With your `awt_...` an attacker can manage and trigger *your* connections, but cannot read a stored key and cannot send it to their own server — replacing a connection always pushes a fresh key that overwrites the old one. If a token leaks: have the operator `revoke` your invite (the token dies at once), re-pair with a fresh code; your machine re-pushes everything.
- **Tenants are invisible to each other** — private workspaces, constant-time token-hash comparison, a 60 req/min per-tenant rate limit.

The one boundary, stated plainly: the hub fires warm-ups *with your key*, so its plaintext passes through the hub process's memory. Whoever operates the hub — or holds root on its box — can read it. Everything above is enforced by design; this last item is a trust decision, the same rule stated under [How It Works](#how-it-works): a hub is exactly as trustworthy as the machine it runs on.

Recommendations:

- Users: delegate a dedicated, revocable key — not the one the rest of your tooling lives on — and rotate it whenever in doubt.
- Users: connect via the hub's `https://` URL only, never a bare `http://host:port`, and keep the client package current.
- Operators: expose the hub only through the cloudflared tunnel (or an equivalent TLS proxy), keep the default `--bind 127.0.0.1`, guard the data dir (`tenants.json` holds invites and tenant tokens in the clear, by design), and keep the package updated.

## Upgrading from pre-split `awewarm serve --hub`

The data dir (`~/.awewarm-server`, or whatever `--data-dir`/the old `hub config --data-dir` set) carries over unchanged — tenants, invites, and the persisted data-dir setting all keep working. Stop the old serve, install this package, start `awewarm-hub serve`. The old spellings (`awewarm serve --hub`, `awewarm hub ...`) die with a tombstone in awewarm naming their replacement here.

Upgrading to v0.5.6 (breaking): `revoke`/`restore` now address invite codes only — `revoke t_...` is gone; find the code a tenant joined with via `list invites --reveal`. The first launch after upgrading migrates `tenants.json` once: tenant-level suspensions move onto their invite's `revokedAt`, and any invite row minted before codes were stored on disk is dropped together with the tenant it produced (its token dies; its workspace stays — re-pair with a fresh invite). Upgrade the package and restart serve together, so both processes speak registry v2.

## Config

`awewarm-hub config` is the settings entry — one command, two concerns. Alone it shows what is in effect and where it comes from: the data dir and the three capacity caps. `--data-dir /data` persists the default data dir on the hub machine (`--unset` clears it); when given with cap flags it instead selects the registry they apply to, once, like the `--data-dir` flag on every other command. The default is `~/.awewarm-server`, shared with awewarm's solo server. Inside it: `tenants.json` (token hashes, invite codes, the serve record with caps/bind/version/start time) and one private `tenants/<id>/` workspace per tenant.

The caps — `--max-tenants`, `--max-conns-per-tenant`, `--max-machines` (`--reset` clears them back to the 10/5/1 defaults) — live in that serve record, the one place every process reads: serve flags stamp them at launch, `config` retunes them at any time, and a running serve adopts the new values without a restart (every tenant action and each scheduling tick re-read the record; one-shot CLI processes read the same numbers, even before a first launch).

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
