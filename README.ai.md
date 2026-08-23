# awewarm-hub Operator Protocol

This document is for AI coding agents. Help the user operate `awewarm-hub`, the multi-tenant hub server for awewarm: one always-on box keeps many users' AI coding-plan subscription windows warm, each user's secrets staying on their own machine.

## Trust and Quota Rules (read this first)

- The hub fires requests with its **users' API keys** — their plaintext keys pass through its RAM. Hub for people who trust the machine's operator (and root). Make sure the user understands this before they invite anyone.
- `awewarm-hub serve` is a **resident process**. Never run or background it from an agent session — it belongs in the user's terminal, tmux, or a systemd unit. The agent's job is everything around it: install, status, invites, tenants.
- Restarting `serve` is safe by design: no secrets live on disk, and each user's machine re-claims and re-pushes its keys on next contact.
- Invite codes (`awi_...`) are one-time secrets (48 h by default). Anyone who can read the data dir — or sees the code — can use a pending invite. Hand them out promptly and privately.

## Language Behavior

- Reply in the user's language when possible.
- If the user asks in Chinese, continue in Chinese.
- If the user asks in English, continue in English.

## Step 1: Install awewarm-hub CLI

### Prerequisites

- Python >= 3.9 (`python3 --version`)
- pip available (`pip --version`)

If Python is missing, tell the user to install it from https://www.python.org/.

### Steps

```bash
pip install awewarm-hub            # brings the awewarm engine with it
awewarm-hub -v
```

Expected output: `awewarm-hub X.Y.Z`

---

## Step 2: Install the awewarm-hub skill

Install the skill so the agent can manage the hub in this and future sessions. Choose one of the following options.

### Option A: Via aweskill (recommended if aweskill is available)

Use this option if the user already has aweskill installed, or is willing to install it. This gives full skill management — install, update, projection, backup.

#### Prerequisites

- Node.js >= 20 (`node --version`) — required by aweskill
- npm available (`npm --version`)

If Node.js is missing, tell the user to install it from https://nodejs.org/.

#### Steps

##### A1. Install aweskill (if not already installed)

```bash
npm install -g aweskill
```

##### A2. Initialize the aweskill central store (if not already done)

```bash
aweskill store init
```

##### A3. Install the awewarm-hub skill from GitHub

```bash
aweskill install wehuman01/awewarm-hub
```

##### A4. Identify the current agent

```bash
aweskill agent supported
```

Look for lines marked with `✓`. Common agent ids: `claude-code`, `cursor`, `codex`, `gemini-cli`, `windsurf`, `opencode`, `qwen-code`.

If you cannot determine the agent id, ask the user.

##### A5. Project the awewarm-hub skill to this agent

```bash
aweskill agent add skill awewarm-hub --global --agent <agent-id>
```

##### A6. Verify

```bash
aweskill agent list --global --agent <agent-id>
```

Expected: `awewarm-hub` shows as `linked`.

---

### Option B: Direct copy (no aweskill needed)

Use this option if the user does not have aweskill and does not want to install Node.js. This copies the SKILL.md file directly into the agent's skill directory.

#### Prerequisites

- `curl` or `wget` available

#### Steps

##### B1. Identify the current agent's skill directory

Determine which agent is running and its global skill directory:

| Agent | Skill directory |
|---|---|
| Claude Code | `~/.claude/skills/awewarm-hub/` |
| Codex | `~/.codex/skills/awewarm-hub/` |
| Cursor | `.cursor/skills/awewarm-hub/` (project-level) |
| Gemini CLI | `~/.gemini/skills/awewarm-hub/` |
| Windsurf | `~/.windsurf/skills/awewarm-hub/` |
| OpenCode | `~/.opencode/skills/awewarm-hub/` |
| Qwen Code | `~/.qwen/skills/awewarm-hub/` |

If the agent is not in this list, ask the user where to place the skill file.

##### B2. Download and place SKILL.md

```bash
mkdir -p <skill-directory>
curl -fsSL https://raw.githubusercontent.com/wehuman01/awewarm-hub/main/resources/skills/awewarm-hub/SKILL.md -o <skill-directory>/SKILL.md
```

Replace `<skill-directory>` with the path from step B1.

---

## Step 3: Check the hub read-only (safe to run)

```bash
awewarm-hub status                 # capacity, invite counts, tenants, serve liveness
awewarm-hub status --details       # + every delegated connection across tenants
awewarm-hub list invites           # every minted code and its fate
```

`status`'s serve-liveness line is best-effort — "NOT reachable" is information, not an error: if no `serve` is running yet, proceed to Step 4. Report the findings to the user.

---

## Step 4: Tell the user to run `awewarm-hub serve`

`serve` is the resident server process — **do not run it yourself, and never background it from an agent session.** Tell the user:

> Run `awewarm-hub serve` in your terminal (or set up the systemd unit from the README). It listens on 127.0.0.1:8790 and keeps its data at ~/.awewarm-server.

For production, point the user at the README's systemd unit and the cloudflared tunnel setup (free TLS, no open inbound ports) — the same shape as a solo awewarm server, with `ExecStart=awewarm-hub serve`. Pairing should happen over https.

---

## Step 5: Invite users (operator side) and onboard them (user side)

Mint one invite per person (one use, 48 h by default):

```bash
awewarm-hub invite --note alice
```

Hand the printed `awi_...` code to that person promptly and privately. Then each **user**, on their own machine with plain open-source awewarm (they never need this package), runs:

```bash
awewarm remote connect https://warm.example.com --invite awi_...
awewarm config set <id> --remote
```

For user-side help, follow awewarm's agent doc: https://github.com/wehuman01/awewarm/blob/main/README.ai.md

Afterwards, check adoption on the hub:

```bash
awewarm-hub status                 # tenants against the max
awewarm-hub list users             # health, usage, machines, joining code
```

---

## Useful commands

Read-only commands (safe to run in agent):

```bash
awewarm-hub status [--details]     # dashboard; --details appends every delegated connection
awewarm-hub list users [--api|--reveal|--json]   # tenants (health, usage, machines)
awewarm-hub list invites [--reveal|--json]       # every minted code and its fate
awewarm-hub config                 # resolved data dir and where it comes from
awewarm-hub self-update --check    # show current/latest version
```

Operator actions (run on user request):

```bash
awewarm-hub invite [--note <who>] [--expires-hours N] [--machines N]   # mint a one-time code
awewarm-hub revoke <awi_...>                    # kill an invite: pending stops pairing, used suspends its tenant (reversible)
awewarm-hub restore <awi_...>                   # undo a revoke
awewarm-hub config --data-dir /data [--unset]   # persist the default data dir
awewarm-hub self-update                         # upgrade awewarm-hub
```

User-only command (resident process — the user runs it in their own terminal or systemd):

```bash
awewarm-hub serve [--data-dir/--bind/--port] [--max-tenants/--max-conns-per-tenant/--max-machines/--tick-seconds]
```

## Safety Rules

- Never run `serve` from the agent, and never background it — it is a resident process the operator owns.
- Invite codes are one-time secrets. Never log them beyond what the command prints for handoff, and remind the user to send them over a private channel.
- Revoke before you delete nothing: revocation is suspension, not deletion, and `restore` undoes it. Both address invite codes (`awi_...`) — a tenant's code is the USED BY match in `list invites --reveal`. Prefer `revoke` for anything reversible.
- Users' API keys pass through the hub's RAM. State the trust rule plainly before the user invites strangers.
- Restarting `serve` is safe (keys re-push automatically); still, coordinate it with the user instead of surprising them.
- If any command fails, report the exact command and error message. Do not silently retry.

## Final Step

After setup, tell the user to invoke skills (`/` in Claude Code, `$` in Codex, or the equivalent in other agents) and check if `awewarm-hub` appears in the list. If it does, the skill is ready to use immediately. If not, the user should restart the agent.

> awewarm-hub is installed and the server is ready. Invoke skills (type `/` or `$` depending on your agent) and look for `awewarm-hub` — if it appears, you're good to go. If not, restart the agent. Then you can ask me things like:
>
> - "Invite bob to the hub."
> - "Who has joined, and how much are they using?"
> - "Suspend alice's tenant while she's on vacation."

If the user is speaking Chinese, use this version instead:

> awewarm-hub 已安装,服务器已就绪。请调用 skills(输入 `/` 或 `$`,取决于你的 agent),看看列表中是否出现了 `awewarm-hub`。如果出现了,说明已就绪可以直接使用。如果没有,请重启 agent 后再试。然后你可以继续问我,例如:
>
> - "邀请 bob 加入 hub。"
> - "现在有哪些人加入了?用量怎么样?"
> - "alice 休假期间先停用她的租户。"

---

## Next Steps

### awewarm — the client side

Hub *users* never need this package — they run plain open-source [awewarm](https://github.com/wehuman01/awewarm) on their own machines. For user-side onboarding (connect with an invite, delegate a connection, check status), follow awewarm's agent doc: https://github.com/wehuman01/awewarm/blob/main/README.ai.md

### aweswitch — agent profile switching

If the user launches coding sessions against different providers, point them to [aweswitch](https://github.com/Webioinfo01/aweswitch), an agent profile switcher for Claude Code, Codex, and OpenCode. aweswitch manages which provider a session launches with; awewarm keeps that provider's subscription window open underneath.
