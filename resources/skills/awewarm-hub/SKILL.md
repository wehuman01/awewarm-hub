---
name: awewarm-hub
description: "Use when helping operators run an awewarm-hub server — starting serve, minting invites, listing tenants, revoking/restoring access, capacity limits, data dirs, or upgrading the hub. 中文触发词：hub、多租户、租户、邀请码、hub 服务器、共享保温服务器、awewarm-hub。"
---

# awewarm-hub

This skill covers **operating** an awewarm-hub server: the resident `serve`, one-time invites, tenants, capacity, and revocation. User-side delegation (connect, `--remote`, `--local`) belongs to the `awewarm` skill.

## Trust Rules (read this first)

- The hub fires requests with its **users' API keys** — their plaintext keys pass through its RAM. Hub for people who trust the machine's operator (and root); a shared VPS with strangers is not that. State this before inviting anyone.
- Invite codes (`awi_...`) are one-time secrets (48 h by default); tenant tokens (`awt_...`) are live credentials. Anyone who can read the data dir can spend a pending invite or act as a tenant — guard it.
- Restarting `serve` is safe by design: API keys never touch disk; each user's machine re-claims and re-pushes its keys on next contact.

## Command Safety

| Category | Commands |
|---|---|
| Read-only — run freely | `awewarm-hub status [--details]`, `awewarm-hub list users [--api\|--reveal\|--json]`, `awewarm-hub list invites [--reveal\|--token\|--json]`, `awewarm-hub config` (no flags = show resolved data dir), `awewarm-hub self-update --check` |
| Admin — run on request | `awewarm-hub invite [--note <who>] [--expires-hours N] [--machines N]` (mints a one-time code), `awewarm-hub revoke <awi_...>` (kill an invite: pending stops pairing, used suspends its tenant — reversible), `awewarm-hub restore <awi_...>` (undo), `awewarm-hub config --data-dir /data [--unset]`, `awewarm-hub self-update` |
| Resident — the operator runs it in their own terminal or systemd, never from an agent session | `awewarm-hub serve [--data-dir/--bind/--port] [--max-tenants/--max-conns-per-tenant/--max-machines/--tick-seconds]` |

## Intent Router

| User intent | Approach |
|---|---|
| "Share one server with my team", "给团队开一个共享 hub" | Operator path: `pip install awewarm-hub`, user runs `awewarm-hub serve`, then `invite` per person. Users pair with plain awewarm: `awewarm remote connect <url> --invite awi_...`. |
| "Invite alice", "邀请一个人" | `awewarm-hub invite --note alice` — hand the printed `awi_...` code to that person promptly and privately. |
| "Who has joined? How much are they using?", "谁加入了/用量" | `awewarm-hub status` then `awewarm-hub list users`; `--details` / `--api` for per-connection detail. |
| "Suspend alice while she's away", "停用一个租户" | `awewarm-hub revoke <her awi_...>` (the USED BY match in `list invites --reveal`) — suspension, not deletion; her token stops authenticating, everything stays on disk, the capacity slot frees. |
| "Bring her back", "恢复" | `awewarm-hub restore <awi_...>` — re-takes a capacity slot, refuses when the hub is full. Machine pairings were never touched. |
| "An invite leaked / was sent to the wrong person", "邀请码泄露" | `awewarm-hub revoke awi_...` kills a pending code on the spot; a used one suspends the tenant it produced. |
| "Give alice a second machine", "加机器额度" | The cap lives on her invite row: raise the `machines` value in `tenants.json` (a running serve adopts disk edits), or mint a fresh code with `invite --machines 2`. |
| "Recover an invite code I already sent", "找回邀请码" | `awewarm-hub list invites --reveal` — codes are kept in the clear for exactly this; guard the data dir. |
| "A user lost their token", "用户 token 丢了" | `awewarm-hub list invites --token` — the TOKEN column behind their used code; hand it back privately and they reconnect with `awewarm remote connect <url> --token <it>` (same tenant, same connections). `—` = the tenant predates tokens on disk → mint a fresh invite instead. |
| "Is the server alive?", "serve 还活着吗" | `awewarm-hub status` — the liveness line is best-effort; "NOT reachable" is information, not an error. |
| "Hub is full", "容量不够" | Restart `serve` with higher `--max-tenants` (default 10, active tenants only) or `--max-conns-per-tenant`; a suspended tenant frees its slot without a restart. |
| "Move the data dir", "迁移数据目录" | `awewarm-hub config --data-dir /data` persists the default (flag overrides once, `--unset` clears); stop serve, move the dir, start again. |
| "Upgrade the hub", "升级" | `awewarm-hub self-update --check` then `self-update`; restart `serve` afterwards. Engine and hub release in lockstep — check awewarm's release too. |
| "A user wants to connect / delegate a plan", "用户侧接入" | User-side: plain awewarm on their machine — `awewarm remote connect <url> --invite awi_...`, then `awewarm config set <id> --remote`. Point the user (or their agent) at the `awewarm` skill. |

## How It Works

- Each tenant gets a private workspace under `tenants/<id>/` — connections, state, RAM keyring, invisible across tenants.
- `tenants.json` keeps invite codes and tenant tokens in the clear so the operator can recover either one already sent (`list invites --reveal` / `--token`); authentication compares a token's SHA-256 hash, so pairings survive restarts.
- API keys never touch disk — they live in server RAM and are re-pushed by each user's machine after a restart.
- Capacity is set at serve time: `--max-tenants` (default 10, counting active tenants only), `--max-conns-per-tenant`, `--max-machines` (the default stamped into each new invite; `invite --machines N` overrides per code). A light per-tenant rate limit (60 requests/minute) stops a looping client.
- The invite code is the one ledger of authorization: revoke/restore address codes only, suspension derives from the code's `revokedAt`, and a revoke never touches machine pairings.
- Registry mutations (join, revoke, restore, usage writes) hold a cross-process transaction lock shared by the resident serve and one-shot CLI processes; the server reloads `tenants.json` when it changes underneath, so CLI-side invites/revokes take effect without a restart.
- The engine (WarmServer, schedule, transport, the HTTP handler core) comes from the `awewarm` pip dependency, pinned to its minor version — the wire protocol stays in lockstep with the open-source client.

## Workflows

### First serve on a box

The user runs the resident process themselves (terminal, tmux, or the systemd unit in the README — `ExecStart=awewarm-hub serve`, `loginctl enable-linger $USER` on headless boxes); expose it over https via a cloudflared tunnel exactly like a solo awewarm server (see awewarm's README → *Remote Server*). Default listen: `127.0.0.1:8790`; default data dir: `~/.awewarm-server`.

```bash
awewarm-hub serve                 # the operator's own terminal
awewarm-hub status                # afterwards: capacity, liveness
```

### Invite a user, then watch them join

```bash
awewarm-hub invite --note alice   # prints awi_... (one use, 48 h)
# alice, on her machine (plain awewarm):
#   awewarm remote connect https://warm.example.com --invite awi_...
#   awewarm config set glm --remote
awewarm-hub list users            # her tenant appears: health, usage, machines
```

### The operator's dashboard

```bash
awewarm-hub status                # tenants vs max, invites by fate, data dir, liveness
awewarm-hub status --details      # + every delegated connection: mode, next due, timezone
awewarm-hub list invites          # every minted code: pending/used/expired/revoked
```

### Revoke and restore

```bash
awewarm-hub list invites --reveal   # find the code (the USED BY column names the tenant)
awewarm-hub revoke awi_...          # pending stops pairing; used suspends its tenant
awewarm-hub restore awi_...         # undo; refuses when the hub is full
```

## Core Rules

1. Never run `awewarm-hub serve` from an agent session — it is a resident process the operator owns.
2. Invite codes are one-time secrets and tenant tokens are live credentials: hand either out promptly, over a private channel; `revoke awi_...` the moment a code leaks.
3. `revoke` is suspension, not deletion — prefer it over anything destructive; `restore` undoes it.
4. Users' plaintext API keys pass through the hub's RAM. State the trust rule before the user invites anyone they don't fully trust.
5. Restarting `serve` is safe (keys re-push automatically) — but coordinate it with the user instead of surprising them.
6. User-side operations (connect, delegate, take back) run plain `awewarm` on the user's machine — defer to the `awewarm` skill.
7. If a command fails, report the exact command and error. Do not silently retry.
