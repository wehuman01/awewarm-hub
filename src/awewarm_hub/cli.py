#!/usr/bin/env python3
"""awewarm-hub CLI: serve, status, config, invite, list, revoke, restore,
self-update. Run on the machine that hosts the hub; hub *users* never need
this package — they pair with the open-source awewarm client:

  awewarm remote connect <url> --invite awi_..."""
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click

from awewarm import remote, schedule
from awewarm.config import die, load_config, save_config, timezone_for
from awewarm.server import ApiError
from awewarm.update_check import get_pypi_latest, version_gte

from . import __version__, display_version
from .engine import Hub, run

DEFAULT_SERVER_DATA_DIR = "~/.awewarm-server"


def _version_callback(ctx, _param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo(display_version())
    ctx.exit()


class WrapGroup(click.Group):
    """Group whose `Commands:` listing wraps long one-liners.

    Same behavior as awewarm's `clickext.WrapGroup`, inlined here on
    purpose: a help-rendering nicety must not move the engine pin.
    Click truncates each description to the terminal width with `...`;
    the full first help paragraph wraps just as well.
    """

    def format_commands(self, ctx, formatter):
        rows = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            rows.append((name, cmd.short_help or _first_paragraph(cmd)))
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)


def _first_paragraph(cmd):
    """A command's first help paragraph collapsed to one line."""
    return " ".join((cmd.help or "").partition("\n\n")[0].split())


@click.group(cls=WrapGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--version", is_flag=True, expose_value=False, is_eager=True,
              callback=_version_callback, help="Show the version and exit.")
def cli():
    """Run a multi-tenant awewarm hub: one box, many invited users."""


def _persisted_server_data_dir():
    """Data dir saved with `awewarm-hub config --data-dir`, if any.

    Lives in awewarm's config.json (the same file the pre-split
    `awewarm hub config` wrote), so an operator upgrading from
    `awewarm serve --hub` keeps their setting. Best-effort on purpose:
    a serve process must start even when the local config is unreadable —
    the flag and the default still work."""
    try:
        config = load_config()
    except SystemExit:
        return None
    value = (config.get("global") or {}).get("serverDataDir")
    return value if isinstance(value, str) and value.strip() else None


def _resolve_server_data_dir(flag):
    """--data-dir flag > the dir saved with `config --data-dir` > default."""
    return flag or _persisted_server_data_dir() or DEFAULT_SERVER_DATA_DIR


@cli.command("serve")
@click.option("--data-dir", default=None, show_default="~/.awewarm-server", help="Directory for the hub registry and tenants (~/.awewarm-server, or the one `config --data-dir` saved).")
@click.option("--bind", default="127.0.0.1", show_default=True, help="Address to listen on.")
@click.option("--port", default=8790, show_default=True, type=int, help="Port to listen on (0 picks a free one).")
@click.option("--max-tenants", "max_tenants", default=10, show_default=True, type=int, help="Cap on active tenants (suspended ones free their slot).")
@click.option("--max-conns-per-tenant", "max_conns_per_tenant", default=5, show_default=True, type=int, help="Delegated connections each tenant may keep.")
@click.option("--max-machines", "max_machines", default=1, show_default=True, type=int, help="Default machine cap stamped into each new invite (override per code: invite --machines).")
@click.option("--tick-seconds", default=60, show_default=True, type=int, help="Seconds between scheduling passes.")
def serve_command(data_dir, bind, port, max_tenants, max_conns_per_tenant, max_machines, tick_seconds):
    """Run the hub server: many users, one-time invites to pair.

    \b
  awewarm-hub serve                          # data at ~/.awewarm-server
  awewarm-hub serve --data-dir /data         # ...or keep the registry elsewhere
  awewarm-hub serve --max-tenants 20 --max-machines 2

Users pair with: awewarm remote connect <url> --invite <code from
`awewarm-hub invite`>. Invite codes and tenant tokens are the only secrets
on disk — kept in the clear so the operator can recover either (list
invites --reveal / --token); API keys live in server RAM only.
    """
    if max_tenants <= 0 or max_conns_per_tenant <= 0 or max_machines <= 0:
        die("--max-tenants, --max-conns-per-tenant, and --max-machines must be greater than 0")
    run(
        _resolve_server_data_dir(data_dir), bind=bind, port=port, tick_seconds=tick_seconds,
        max_tenants=max_tenants, max_conns_per_tenant=max_conns_per_tenant,
        max_machines=max_machines,
    )


@cli.command("config")
@click.option("--data-dir", default=None, help="Set the data dir `serve` and hub commands use on this machine by default.")
@click.option("--unset", "unset_dir", is_flag=True, help="Forget the setting; fall back to the default (~/.awewarm-server).")
def config_command(data_dir, unset_dir):
    """Show or set the default data dir for serve and hub commands.

    \b
      awewarm-hub config                    # show the resolved data dir
      awewarm-hub config --data-dir /data   # persist it (a --data-dir flag
                                             #   still overrides once)
    """
    if data_dir and unset_dir:
        die("pass either --data-dir or --unset, not both")
    if unset_dir:
        config = load_config()
        (config.get("global") or {}).pop("serverDataDir", None)
        save_config(config)
        click.echo(f"✓ Server data dir reset to the default: {DEFAULT_SERVER_DATA_DIR}")
        return
    if data_dir:
        resolved = str(Path(data_dir).expanduser())
        config = load_config()
        config.setdefault("global", {})["serverDataDir"] = resolved
        save_config(config)
        click.echo(f"✓ Server data dir set to {resolved}")
        click.echo("  awewarm-hub commands on this machine use it unless --data-dir is passed")
        return
    persisted = _persisted_server_data_dir()
    click.echo(f"data dir: {_resolve_server_data_dir(None)} "
               f"({'set with: awewarm-hub config --data-dir' if persisted else 'the default'})")
    click.echo("  used by awewarm-hub commands; override once with --data-dir")


def _probe_serve(record):
    """Best-effort /healthz against the endpoint the serve launch recorded.
    Unreachable is itself the answer `status` reports — never fatal."""
    if not record or not record.get("port"):
        return None, None
    bind = record.get("bind") or "127.0.0.1"
    host = "127.0.0.1" if bind in ("", "0.0.0.0", "::", "[::]") else bind
    url = f"http://{host}:{record['port']}"
    try:
        return url, remote.healthz(url, timeout=1.5)
    except remote.RemoteError:
        return url, None


@cli.command("status")
@click.option("--data-dir", default=None, help="The hub's data directory (default: ~/.awewarm-server, or the one `config --data-dir` saved).")
@click.option("--details", "show_details", is_flag=True,
              help="Also list every delegated connection with its mode and next due moment.")
def status_command(data_dir, show_details):
    """Hub overview: capacity, invites, tenants, and the serve process.

    \b
      awewarm-hub status             one-glance dashboard
      awewarm-hub status --details   plus every connection of every tenant
    """
    data_dir = _resolve_server_data_dir(data_dir)
    engine = Hub(data_dir)
    rows = engine.summarize()
    invite_counts = {}
    for invite_row in engine.list_invites():
        invite_counts[invite_row["status"]] = invite_counts.get(invite_row["status"], 0) + 1
    active = sum(1 for row in rows if not row["suspended"])
    suspended = len(rows) - active
    total_conns = sum(len(row["connections"]) for row in rows)
    record = engine.serve_record

    click.echo("awewarm-hub")
    if record.get("maxTenants") is not None:
        click.echo(f"  tenants: {active}/{record['maxTenants']} active")
    else:
        click.echo(f"  tenants: {active} active (caps unknown — recorded when serve launches)")
    if suspended:
        click.echo(f"             {suspended} suspended (slot free; restore their invite: awewarm-hub restore <awi_...>)")
    conn_cap = record.get("maxConnsPerTenant")
    click.echo(
        f"  connections: {total_conns} delegated"
        + (f" (max {conn_cap} per tenant)" if conn_cap else "")
    )
    machine_cap = record.get("maxMachines")
    paired = sum(row["machines"] for row in rows if not row["suspended"])
    click.echo(
        f"  machines: {paired} paired"
        + (f" (default {machine_cap} per invite; set per code: awewarm-hub invite --machines)" if machine_cap else " (cap unknown)")
    )
    invites_line = ", ".join(f"{invite_counts[k]} {k}" for k in ("pending", "used", "expired", "revoked") if invite_counts.get(k))
    click.echo(f"  invites: {invites_line or 'none minted'} (mint: awewarm-hub invite)")
    if not record:
        click.echo("  serve: never launched against this data dir")
    else:
        probe_url, probe = _probe_serve(record)
        if probe:
            mode = "hub" if probe.get("hub") else "single-tenant"
            click.echo(f"  serve: reachable at {probe_url} (v{probe.get('version')}, {mode})")
        else:
            click.echo(f"  serve: NOT reachable at {probe_url} — is `awewarm-hub serve` running?")
    click.echo(f"  data dir: {data_dir} (log: {Path(engine.log_path).name})")

    if not rows:
        click.echo("\nNo tenants paired yet — hand out: awewarm-hub invite")
        return
    now = datetime.now().astimezone()
    tenant_rows = []
    for row in rows:
        conns = row["connections"]
        usage = row["usage"] or {}
        seen = _fmt_moment(schedule.parse_ts(row["lastSeenAt"]), now) if row["lastSeenAt"] else "never"
        tenant_rows.append([
            row["tenant"],
            row["note"] or "—",
            str(len(conns)),
            "suspended" if row["suspended"] else _tenant_status(conns),
            str(usage.get("today", 0)),
            seen,
        ])
    click.echo()
    _print_table(["TENANT", "NOTE", "CONNS", "STATUS", "TODAY", "LAST SEEN"], tenant_rows)

    conn_rows = []
    for row in rows:
        for entry in row["connections"]:
            conn_rows.append([
                row["tenant"],
                entry["id"],
                entry["status"],
                entry.get("mode") or "fixed",
                _conn_moment(entry, now),
                entry.get("timezone") or "—",
            ])
    if show_details:
        if conn_rows:
            click.echo()
            _print_table(["TENANT", "CONNECTION", "STATUS", "MODE", "NEXT DUE", "TIMEZONE"], conn_rows)
        else:
            click.echo("\nNo delegated connections yet")
    elif conn_rows:
        click.echo("\nper-connection detail: awewarm-hub status --details")


def _parse_duration(raw):
    """`--expires-in` syntax, the same spelling aweshare's hub CLI uses:
    <int><s|m|h|d>, e.g. 90s, 30m, 12h, 7d."""
    match = re.fullmatch(r"(\d+)([smhd])", raw or "")
    if not match:
        die(f"--expires-in must look like 90s, 30m, 12h or 7d, got '{raw}'")
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = int(match.group(1)) * scale
    if seconds <= 0:
        die("--expires-in must be greater than 0")
    return timedelta(seconds=seconds)


@cli.command("invite")
@click.option("--data-dir", default=None, help="The hub's data directory (default: ~/.awewarm-server, or the one `config --data-dir` saved).")
@click.option("--name", default=None, help="Who these invites are for (shown in list users).")
@click.option("--count", type=int, default=1, show_default=True, help="How many codes to mint at once (they share name, expiry, and machine cap).")
@click.option("--expires-in", "expires_in", default="48h", show_default=True, help="How long each code stays usable: <N><s|m|h|d>, e.g. 90s, 30m, 12h, 7d.")
@click.option("--machines", "machines", type=int, default=None, help="Machines each invite's token may serve from (default: the serve --max-machines value).")
def invite_command(data_dir, name, count, expires_in, machines):
    """Mint one-time pairing invites (recover later with: list invites --reveal)."""
    if not 1 <= count <= 100:
        die(f"--count must be an integer between 1 and 100, got '{count}'")
    if machines is not None and machines <= 0:
        die("--machines must be greater than 0")
    ttl = _parse_duration(expires_in)
    engine = Hub(_resolve_server_data_dir(data_dir))
    try:
        codes = engine.mint_invites(name, ttl, machines=machines, count=count)
    except ApiError as exc:  # registry busy (serve mid-update) or the save failed
        die(f"could not mint the invite:\n{exc}")
    cap = machines if machines is not None else engine.max_machines
    if count == 1:
        click.echo(f"✓ Invite minted{f' for {name}' if name else ''} — one use, expires in {expires_in}, {cap} machine(s)")
    else:
        click.echo(f"✓ {count} invites minted{f' for {name}' if name else ''} — one use each, expires in {expires_in}, {cap} machine(s)")
    for code in codes:
        click.echo(f"  {code}")
    if count == 1:
        click.echo("  The user runs: awewarm remote connect <hub-url> --invite " + codes[0])
        click.echo("  Lost it? List every minted code with: awewarm-hub list invites --reveal")
    else:
        click.echo("  Each user runs: awewarm remote connect <hub-url> --invite <one of the codes above>")
        click.echo("  Lost one? List every minted code with: awewarm-hub list invites --reveal")


@cli.group("list", cls=WrapGroup)
def list_group():
    """Read hub state: users (paired tenants) or invites (minted codes)."""


@list_group.command("users")
@click.option("--data-dir", default=None, help="The hub's data directory (default: ~/.awewarm-server, or the one `config --data-dir` saved).")
@click.option("--api", "show_api", is_flag=True, help="Also list each connection's API endpoint, protocol, and model.")
@click.option("--reveal", "show_codes", is_flag=True, help="Show the full invite code each tenant joined with (masked by default).")
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON (still redacted).")
def list_users_command(data_dir, show_api, show_codes, as_json):
    """Show tenants: pairing, connections, and activation usage."""
    data_dir = _resolve_server_data_dir(data_dir)
    engine = Hub(data_dir)
    rows = engine.summarize()
    pending = sum(1 for row in engine.list_invites() if row["status"] == "pending")
    if as_json:
        out = [dict(row, invite=(row["invite"] if show_codes else _mask_invite(row["invite"]))) for row in rows]
        click.echo(json.dumps(out, indent=2))
        return
    if not rows:
        if pending:
            click.echo(
                f"No tenants paired yet — {pending} invite(s) minted and unused\n"
                "  (see them with: awewarm-hub list invites)"
            )
        else:
            click.echo(f"No tenants paired yet — mint invites with: awewarm-hub invite --data-dir {data_dir}")
        return
    now = datetime.now().astimezone()
    tenant_rows = []
    for row in rows:
        conns = row["connections"]
        usage = row["usage"] or {}
        seen = _fmt_moment(schedule.parse_ts(row["lastSeenAt"]), now) if row["lastSeenAt"] else "never"
        paired = schedule.parse_ts(row["createdAt"])
        tenant_rows.append([
            row["tenant"],
            row["note"] or "—",
            (row["invite"] or "—") if show_codes else _mask_invite(row["invite"]),
            str(len(conns)),
            "suspended" if row["suspended"] else _tenant_status(conns),
            str(usage.get("today", 0)),
            str(usage.get("total", 0)),
            seen,
            paired.strftime("%Y-%m-%d") if paired else "—",
        ])
    _print_table(
        ["TENANT", "NOTE", "INVITE", "CONNS", "STATUS", "TODAY", "TOTAL", "LAST SEEN", "PAIRED"],
        tenant_rows,
    )
    if show_api:
        api_rows = []
        for row in rows:
            for entry in row["connections"]:
                api_rows.append([
                    row["tenant"],
                    entry["id"],
                    entry["status"],
                    entry["protocol"] or "—",
                    entry["model"] or "—",
                    _conn_moment(entry, now),
                    entry["api"] or "—",
                ])
        if api_rows:
            click.echo()
            _print_table(
                ["TENANT", "CONNECTION", "STATUS", "PROTOCOL", "MODEL", "NEXT DUE", "API"],
                api_rows,
            )
        else:
            click.echo("\nNo delegated connections yet")
    if not show_codes and any(row["invite"] for row in rows):
        click.echo("\ncodes are masked — pass --reveal to show them")
    footer = f"\n{len(rows)} tenant(s)"
    if pending:
        footer += f", {pending} unused invite(s) pending"
    click.echo(footer)


@list_group.command("invites")
@click.option("--data-dir", default=None, help="The hub's data directory (default: ~/.awewarm-server, or the one `config --data-dir` saved).")
@click.option("--reveal", "show_codes", is_flag=True, help="Show full invite codes instead of masked ones (pending codes still work).")
@click.option("--token", "show_tokens", is_flag=True, help="Also show each used invite's tenant token — hand one back to a user who lost it (— for tenants that predate tokens on disk).")
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON (codes follow --reveal, tokens --token).")
def list_invites_command(data_dir, show_codes, show_tokens, as_json):
    """List minted invites: code, status, expiry, and who joined with it."""
    engine = Hub(_resolve_server_data_dir(data_dir))
    rows = engine.list_invites()
    if as_json:
        out = [
            dict(row,
                 code=(row["code"] if show_codes else _mask_invite(row["code"])),
                 token=(row["token"] if show_tokens else _mask_invite(row["token"])))
            for row in rows
        ]
        click.echo(json.dumps(out, indent=2))
        return
    if not rows:
        click.echo("No invites minted yet — mint one with: awewarm-hub invite --name <who>")
        return
    now = datetime.now().astimezone()
    headers = ["NOTE", "CODE", "STATUS", "MACHINES", "EXPIRES", "USED BY", "USED AT", "MINTED"]
    if show_tokens:
        headers.append("TOKEN")
    table_rows = []
    for row in rows:
        expires = schedule.parse_ts(row["expiresAt"])
        created = schedule.parse_ts(row["createdAt"])
        used = schedule.parse_ts(row["usedAt"]) if row["usedAt"] else None
        cells = [
            row["note"] or "—",
            (row["code"] or "—") if show_codes else _mask_invite(row["code"]),
            row["status"],
            str(row["machines"]) if row["machines"] else "global",
            expires.strftime("%m-%d %H:%M") if expires else "—",
            row["usedBy"] or "—",
            used.strftime("%m-%d %H:%M") if used else "—",
            created.strftime("%m-%d %H:%M") if created else "—",
        ]
        if show_tokens:
            cells.append(row["token"] or "—")
        table_rows.append(cells)
    _print_table(headers, table_rows)
    if not show_codes:
        click.echo("codes are masked — pass --reveal to show them (a pending code still pairs)")
    elif any(row["code"] is None for row in rows):
        click.echo("codes shown as — were minted before codes were kept on disk; mint a fresh invite for those")
    if not show_tokens and any(row["token"] for row in rows):
        click.echo("tenant tokens hidden — pass --token to show them (a user reconnects with: awewarm remote connect <url> --token <it>)")
    elif show_tokens and any(row["usedBy"] and not row["token"] for row in rows):
        click.echo("tokens shown as — predate tokens being kept on disk; those users re-pair with a fresh invite")
    if any(row["machines"] is None for row in rows):
        click.echo("MACHINES 'global' = minted before per-invite caps; follows the live serve --max-machines")


def _require_code_target(target):
    """The invite code is the only handle for revoke/restore — tenants are
    not addressable directly anymore."""
    if target.startswith("t_"):
        die(
            "tenants are no longer addressable directly — operate on their invite instead\n"
            "  find it with: awewarm-hub list invites --reveal (the USED BY column names the tenant)"
        )


@cli.command("revoke")
@click.argument("code")
@click.option("--data-dir", default=None, help="The hub's data directory (default: ~/.awewarm-server, or the one `config --data-dir` saved).")
@click.option("--delete", "hard_delete", is_flag=True,
              help="Remove the invite from the ledger outright — no revoked row stays. A used one takes its tenant with it (token dead, capacity freed, workspace kept on disk). Irreversible.")
def revoke_command(code, data_dir, hard_delete):
    """Kill an invite (awi_...): a pending code stops pairing, a used one
    suspends its tenant. Reversible.

    --delete wipes the ledger row instead — no revoked tombstone; a used
    one takes its tenant with it. Irreversible."""
    _require_code_target(code)
    engine = Hub(_resolve_server_data_dir(data_dir))
    known = {row["code"]: row for row in engine.list_invites() if row["code"]}
    row = known.get(code)
    if row is None:
        die(f"no such invite: {code}\nfix: list codes with: awewarm-hub list invites --reveal")
    note = f" for {row['note']}" if row["note"] else ""
    if hard_delete:
        if row["usedBy"]:
            prompt = (
                f"Delete invite{note} and its tenant {row['usedBy']} from the ledger?\n"
                "The token stops authenticating, the capacity slot frees, and no\n"
                "revoked row remains — the workspace stays on disk. Irreversible."
            )
        else:
            prompt = (
                f"Delete invite{note} from the ledger? The code never pairs, and no\n"
                "revoked row remains. Irreversible."
            )
        if not click.confirm(prompt, default=False):
            click.echo("aborted — nothing deleted")
            return
        try:
            result = engine.delete_invite(code)
        except ApiError as exc:  # registry busy or unknown
            die(f"could not delete the invite:\n{exc}")
        if result.get("tenant"):
            click.echo(f"✓ invite deleted{note} — tenant {result['tenant']} removed (its workspace stays on disk)")
        else:
            click.echo(f"✓ invite deleted{note} — no revoked row remains")
        click.echo("  a fresh invite: awewarm-hub invite")
        return
    if row["usedBy"]:
        prompt = f"Revoke invite{note}? Tenant {row['usedBy']} stops working immediately."
    else:
        prompt = f"Revoke invite{note}? It stops pairing immediately."
    if not click.confirm(prompt, default=False):
        click.echo("aborted — nothing revoked")
        return
    try:
        result = engine.revoke(code)
    except ApiError as exc:  # registry busy, unknown, or already revoked
        die(f"could not revoke the invite:\n{exc}")
    status = result["status"]
    if status == "used":
        click.echo(f"✓ invite revoked{note} — tenant {result['tenant']} suspended, its token no longer works")
    elif status == "expired":
        click.echo(f"✓ invite revoked{note} (it had already expired — the ledger row is kept)")
    else:
        click.echo(f"✓ invite revoked{note} — the code no longer pairs; a fresh one: awewarm-hub invite")
    click.echo(f"  undo: awewarm-hub restore {code}")


@cli.command("restore")
@click.argument("code")
@click.option("--data-dir", default=None, help="The hub's data directory (default: ~/.awewarm-server, or the one `config --data-dir` saved).")
def restore_command(code, data_dir):
    """Undo a revoke: a pending invite (awi_...) pairs again, a used one brings its tenant back."""
    _require_code_target(code)
    engine = Hub(_resolve_server_data_dir(data_dir))
    try:
        result = engine.restore(code)
        if result["tenant"]:
            click.echo(f"✓ invite restored — tenant {result['tenant']} is back (capacity permitting)")
        else:
            click.echo("✓ invite restored — the code pairs again")
            click.echo("  it still obeys its original expiry; a fresh one: awewarm-hub invite")
    except ApiError as exc:  # registry busy, unknown, not revoked, or hub full
        die(f"could not restore {code}:\n{exc}")


@cli.command("self-update")
@click.option("--check", "check_only", is_flag=True, help="Show versions without updating.")
def self_update_command(check_only):
    """Update awewarm-hub to the latest PyPI release."""
    try:
        latest = get_pypi_latest("awewarm-hub")
    except Exception as exc:
        die(f"failed to check PyPI: {exc}")
    if version_gte(__version__, latest):
        click.echo(f"awewarm-hub is up to date ({__version__}).")
        return
    click.echo(f"Current: {__version__}  Latest: {latest}")
    if check_only:
        return
    from . import running_from_checkout
    if running_from_checkout():
        die("this awewarm-hub runs from a source checkout (pip install -e .) — "
            "update it with: git pull && pip install -e .")

    if Path(sys.prefix, "pyvenv.cfg").exists() and "pipx" in sys.prefix:
        cmd = [shutil.which("pipx") or "pipx", "upgrade", "awewarm-hub"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "awewarm-hub"]

    click.echo(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    click.echo("Done. Restart `awewarm-hub serve` to run the new version.")


# --- shared output helpers ---


def _fmt_moment(moment, now):
    if moment is None:
        return "never"
    if moment.date() == now.date():
        return f"today {moment.strftime('%H:%M')}"
    return moment.strftime("%Y-%m-%d %H:%M")


def _mask_invite(code):
    if not code:
        return "—"
    return code if len(code) <= 8 else code[:8] + "…"


def _print_table(headers, rows):
    """Left-aligned table, two-space gutters, widths from the content."""
    widths = [
        max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
        for i, header in enumerate(headers)
    ]
    click.echo("  ".join(header.ljust(w) for header, w in zip(headers, widths)))
    for row in rows:
        click.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


def _tenant_status(conns):
    """Worst health rung across a tenant's connections. Key presence is
    deliberately absent: RAM keys are only knowable inside the serve process,
    not from this one — tenants see it via their own `status --remote`."""
    for word in ("invalid", "auto-disabled", "degraded", "failing"):
        if any(c["status"] == word for c in conns):
            return word
    return "connected" if conns else "—"


def _conn_moment(entry, now):
    due_at = schedule.parse_ts(entry.get("nextDueAt"))
    if due_at is None:
        return "—"
    try:
        conn_now = datetime.now(timezone_for(entry.get("timezone"))) if entry.get("timezone") else now
    except Exception:
        conn_now = now
    return _fmt_moment(due_at, conn_now)


def main(argv=None):
    """Console entry point."""
    args = list(sys.argv[1:] if argv is None else argv)
    return cli.main(args=args, prog_name="awewarm-hub")
