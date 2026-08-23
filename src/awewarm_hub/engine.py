"""awewarm-hub engine: tenants, invites, and quotas over awewarm's WarmServer.

Each tenant gets a private WarmServer workspace under `tenants/<id>/` — its
connections, state, and RAM keyring are invisible to other tenants by
construction. Pairing flows through one-time invites minted by the operator
(`awewarm-hub invite`); /v1/join burns one and returns a personal token.
tenants.json keeps invite codes and tenant tokens in the clear so the
operator can recover either one already sent (`awewarm-hub list invites
--reveal` / `--token`); authentication compares the token's SHA-256 hash.
API keys still never touch disk, and the pairings survive a restart without
waiting for every user to come back online.

This module leans on awewarm's semi-public server surface (WarmServer,
ApiError, schedule, the config/locking helpers). Its dependency pins this
package to awewarm's minor version; awewarm refactors must keep those names
importable and behaving.

Engine knobs (`awewarm-hub serve`); the capacity caps are serve flags,
stamped into the registry at launch so `awewarm-hub status` can report
them, and the machine cap they set is the default stamped into each new
invite (`invite --machines` overrides per code).
"""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from awewarm import __version__ as awewarm_version, schedule
from awewarm.config import append_log, conn_state, _write_json
from awewarm.locking import LockBusy, process_lock
from awewarm.server import ApiError, WarmServer

from . import __version__
from .handler import HubHandler

INVITE_RE = re.compile(r"^awi_[A-Za-z0-9_-]{16,128}$")
DEFAULT_MAX_TENANTS = 10
DEFAULT_MAX_CONNS_PER_TENANT = 5
DEFAULT_MAX_MACHINES = 1
INVITE_TTL_HOURS = 48
# Generous for honest clients (status + sync make a handful of calls an hour)
# while still stopping a looping client from monopolizing the process.
HUB_RATE_PER_MINUTE = 60
# Persisting lastSeen on every request would rewrite tenants.json constantly;
# refreshing it at most this often keeps `awewarm-hub list users` honest to a small window.
HUB_SEEN_PRECISION = timedelta(minutes=10)


class Tenant:
    """One paired hub user: registry bookkeeping plus a private workspace.

    The workspace is that tenant's own WarmServer over `tenants/<id>/` — its
    connections, state, and RAM keyring are invisible to other tenants by
    construction. It loads lazily so `awewarm-hub invite` / `list` never pay
    for spinning up every tenant's files.
    """

    def __init__(self, tenant_id, record, tenants_root):
        self.id = tenant_id
        self.record = record  # the registry entry: tokenHash, note, createdAt, lastSeenAt, usage, machines
        self.workspace_dir = Path(tenants_root) / tenant_id
        self.requests = deque()  # monotonic timestamps, the rate-limit window
        self._warm = None

    @property
    def token_hash(self):
        return self.record.get("tokenHash") or ""

    @property
    def note(self):
        return self.record.get("note") or ""

    @property
    def warm(self):
        if self._warm is None:
            self._warm = WarmServer(self.workspace_dir)
        return self._warm


class Hub:
    """The multi-tenant engine behind `awewarm-hub serve`.

    Pairing flows through one-time invites minted by the operator
    (`awewarm-hub invite`); /v1/join burns one and returns a personal token.
    tenants.json keeps invite codes and tenant tokens in the clear so the
    operator can recover either one already sent (`awewarm-hub list invites
    --reveal` / `--token`); authentication compares the token's SHA-256
    hash. API keys still never touch disk, and unlike single-tenant mode
    the pairings survive a restart without waiting for every user to come
    back online.
    """

    def __init__(self, data_dir, max_tenants=None, max_conns_per_tenant=None, max_machines=None):
        self.data_dir = Path(data_dir).expanduser()
        self.registry_path = self.data_dir / "tenants.json"
        self.registry_lock_path = self.data_dir / "tenants.lock"
        self.log_path = self.data_dir / "awewarm-hub.log"
        self.lock = threading.RLock()
        self.registry = self._load()
        self._migrate_registry()
        # Serve always passes its flag values; one-shot CLI processes
        # (awewarm-hub invite / status) pass None and adopt what the running
        # serve stamped into the registry — so `awewarm-hub status` reports
        # the live caps, not the defaults. Nothing recorded yet falls back to
        # the code defaults.
        self.serve_record = self.registry.get("serve") or {}
        self.max_tenants = (
            max_tenants if max_tenants is not None
            else self.serve_record.get("maxTenants", DEFAULT_MAX_TENANTS)
        )
        self.max_conns_per_tenant = (
            max_conns_per_tenant if max_conns_per_tenant is not None
            else self.serve_record.get("maxConnsPerTenant", DEFAULT_MAX_CONNS_PER_TENANT)
        )
        self.max_machines = (
            max_machines if max_machines is not None
            else self.serve_record.get("maxMachines", DEFAULT_MAX_MACHINES)
        )
        self._registry_stamp = self._stamp()
        self.tenants = {
            tenant_id: Tenant(tenant_id, record, self.data_dir / "tenants")
            for tenant_id, record in self.registry["tenants"].items()
        }

    # --- registry (invite codes + tenant tokens in the clear for recovery;
    # API keys never land here) ---

    def _load(self):
        try:
            data = json.loads(self.registry_path.read_text())
        except FileNotFoundError:
            return {"version": 2, "tenants": {}, "invites": {}}
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"awewarm-hub: cannot read {self.registry_path}\n{exc}\n"
                "fix: delete the file — tenants must re-join with fresh invites"
            )
        if not isinstance(data, dict) or not isinstance(data.get("tenants"), dict):
            raise SystemExit(
                f"awewarm-hub: {self.registry_path} is malformed\n"
                "fix: delete the file — tenants must re-join with fresh invites"
            )
        data.setdefault("invites", {})
        return data

    def _migrate_registry(self):
        """One-shot v1→v2 upgrade: the invite becomes the only authority on
        authorization. v1 kept a `suspendedAt` mirror on tenants and allowed
        invite rows without a plaintext code; v2 derives suspension from the
        invite's `revokedAt` and addresses revoke/restore by code only. A row
        that predates stored codes is unaddressable now — it goes, together
        with the tenant it produced (whose token would otherwise live on,
        beyond revoking); the tenant's workspace stays on disk. Runs under the
        process lock so a concurrent serve transaction cannot interleave."""
        if self.registry.get("version", 1) >= 2:
            return
        try:
            with process_lock(self.registry_lock_path, timeout_seconds=5):
                fresh = self._load()  # re-read under the lock; another process may have migrated
                if fresh.get("version", 1) < 2:
                    for digest, entry in list(fresh["invites"].items()):
                        if entry.get("code"):
                            continue
                        used_by = entry.get("usedBy")
                        del fresh["invites"][digest]
                        if used_by:
                            fresh["tenants"].pop(used_by, None)
                    for tenant_id, record in fresh["tenants"].items():
                        suspended = record.pop("suspendedAt", None)
                        if suspended:
                            invite = next(
                                (e for e in fresh["invites"].values() if e.get("usedBy") == tenant_id),
                                None,
                            )
                            if invite is not None:
                                invite.setdefault("revokedAt", suspended)
                    fresh["version"] = 2
                    _write_json(self.registry_path, fresh)
                self.registry = fresh
        except LockBusy:
            raise SystemExit("awewarm-hub: hub registry is busy — retry this command")

    def _stamp(self):
        """Identity of tenants.json on disk; any write — ours or another
        process's — changes it."""
        try:
            stat = self.registry_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _save(self):
        _write_json(self.registry_path, self.registry)
        self._registry_stamp = self._stamp()

    @contextmanager
    def _registry_transaction(self):
        """Serialize refresh + mutation + save across serve and hub CLI processes."""
        with self.lock:
            try:
                with process_lock(self.registry_lock_path, timeout_seconds=5):
                    self._refresh()
                    yield
            except LockBusy:
                raise ApiError(503, "hub registry is busy — retry this request")

    def _refresh(self):
        """Adopt tenants.json changes made by other processes since our last look.

        `awewarm-hub invite` and `awewarm-hub revoke` are one-shot CLI processes
        writing the same file; without this a long-lived serve would 403 every
        join with an invite minted after it started and keep honoring revoked
        tokens. Disk wins for persisted tenant records because every mutation
        saves synchronously under the registry transaction lock. Existing Tenant
        objects stay alive so their RAM keyrings and rate-limit queues survive."""
        with self.lock:
            stamp = self._stamp()
            if stamp is None or stamp == self._registry_stamp:
                return
            try:
                fresh = self._load()
            except SystemExit as exc:
                # a bad file mid-request must not kill the serve thread;
                # keep answering with what we have and say so in the log
                self.log(f"registry reload skipped: {exc}")
                return
            self.registry = fresh
            for tenant_id in list(self.tenants):
                if tenant_id in fresh["tenants"]:
                    self.tenants[tenant_id].record = fresh["tenants"][tenant_id]
                else:  # revoked by the operator in another process
                    self.tenants.pop(tenant_id, None)
            for tenant_id, record in fresh["tenants"].items():
                if tenant_id not in self.tenants:
                    self.tenants[tenant_id] = Tenant(tenant_id, record, self.data_dir / "tenants")
            self._registry_stamp = stamp

    def log(self, message):
        append_log(self.log_path, message)

    def record_launch(self, bind, port):
        """Stamp the effective knobs into the registry so `awewarm-hub status`
        (a separate process) can report caps and find the live endpoint. Last
        launch wins; nothing here is secret."""
        record = {
            "version": __version__,
            "awewarmVersion": awewarm_version,
            "startedAt": schedule.iso(datetime.now().astimezone()),
            "bind": bind,
            "port": port,
            "maxTenants": self.max_tenants,
            "maxConnsPerTenant": self.max_conns_per_tenant,
            "maxMachines": self.max_machines,
        }
        with self._registry_transaction():
            self.registry["serve"] = record
            self._save()
        self.serve_record = record
        self.log(
            f"serve up on {bind}:{port} — caps {self.max_tenants} tenants, "
            f"{self.max_conns_per_tenant} conns each, default {self.max_machines} machine(s) per invite"
        )

    # --- pairing ---

    def mint_invites(self, note=None, ttl=timedelta(hours=INVITE_TTL_HOURS), machines=None, count=1):
        """One-time pairing codes, all in one registry transaction (`invite
        --count`); each code is kept so `list invites` can recover it (the
        tenant token each one produces is kept the same way for `--token`).
        `machines` is the machine cap every code carries — the cap is
        a property of the authorization, so it is stamped here; the default
        is the global one (`serve --max-machines`)."""
        now = datetime.now().astimezone()
        codes = []
        with self._registry_transaction():
            for _ in range(count):
                invite = "awi_" + secrets.token_urlsafe(16)
                self.registry["invites"][_hash_secret(invite)] = {
                    "code": invite,
                    "note": note,
                    "machines": machines if machines is not None else self.max_machines,
                    "createdAt": schedule.iso(now),
                    "expiresAt": schedule.iso(now + ttl),
                }
                codes.append(invite)
            self._save()
        return codes

    def mint_invite(self, note=None, ttl=timedelta(hours=INVITE_TTL_HOURS), machines=None):
        """One invite — the common case over mint_invites (same semantics)."""
        return self.mint_invites(note, ttl=ttl, machines=machines)[0]

    def list_invites(self):
        """Rows for `awewarm-hub list invites` — every minted code and its fate."""
        now = datetime.now().astimezone()
        rows = []
        for entry in self.registry["invites"].values():
            expires = schedule.parse_ts(entry.get("expiresAt"))
            used_by = entry.get("usedBy")
            if entry.get("revokedAt"):
                status = "revoked"  # pending or used — the code is the one ledger of both
            elif used_by:
                status = "used"
            elif expires is not None and expires <= now:
                status = "expired"
            else:
                status = "pending"
            rows.append({
                # absent on invites minted before codes were kept on disk
                "code": entry.get("code"),
                "note": entry.get("note"),
                # absent on invites minted before the cap moved onto the code;
                # those follow the live global cap instead
                "machines": entry.get("machines"),
                "createdAt": entry.get("createdAt"),
                "expiresAt": entry.get("expiresAt"),
                "usedBy": used_by,
                "usedAt": entry.get("usedAt"),
                "status": status,
                # the token this code's tenant authenticates with, for
                # `list invites --token`; absent on tenants that joined
                # before tokens were kept on disk
                "token": (self.registry["tenants"].get(used_by) or {}).get("token"),
            })
        rows.sort(key=lambda row: row["createdAt"] or "")
        return rows

    def join(self, invite, machine=None):
        """Burn one invite, create the tenant, and return its token exactly once."""
        with self._registry_transaction():
            if not INVITE_RE.match(invite or ""):
                raise ApiError(400, "invite must look like awi_<code> — get one from the hub operator")
            digest = _hash_secret(invite)
            entry = self.registry["invites"].get(digest)
            if entry is None or entry.get("usedBy"):
                raise ApiError(403, "unknown or already-used invite — ask the hub operator for a fresh one")
            if entry.get("revokedAt"):
                raise ApiError(403, "invite revoked by the operator — ask them to restore it before reusing the code")
            now = datetime.now().astimezone()
            if schedule.parse_ts(entry.get("expiresAt")) <= now:
                del self.registry["invites"][digest]
                self._save()
                raise ApiError(403, "invite expired — ask the hub operator for a fresh one")
            self._require_capacity()
            tenant_id = "t_" + secrets.token_hex(4)
            while tenant_id in self.registry["tenants"]:
                tenant_id = "t_" + secrets.token_hex(4)
            token = "awt_" + secrets.token_urlsafe(32)
            self.registry["tenants"][tenant_id] = {
                "tokenHash": _hash_secret(token),
                # in the clear so the operator can hand a lost token back
                # (`list invites --token`); auth compares the hash above
                "token": token,
                "note": entry.get("note"),
                "createdAt": schedule.iso(now),
                "lastSeenAt": None,
                "usage": {"day": None, "today": 0, "total": 0},
                "machines": [machine] if machine else [],
            }
            self.tenants[tenant_id] = Tenant(tenant_id, self.registry["tenants"][tenant_id], self.data_dir / "tenants")
            entry["usedBy"] = tenant_id
            entry["usedAt"] = schedule.iso(now)
            self._save()
            self.log(f"{tenant_id} joined ({self.tenants[tenant_id].note or 'no note'})")
            return {"ok": True, "token": token, "tenantId": tenant_id}

    def _require_capacity(self):
        """Tenants behind a revoked invite free their slot; taking one back
        needs room."""
        active = sum(1 for tenant_id in self.registry["tenants"] if not self._suspension_of(tenant_id))
        if active >= self.max_tenants:
            raise ApiError(
                403,
                f"hub is full ({self.max_tenants} active tenants) — "
                "the operator must revoke an invite first: awewarm-hub list invites --reveal",
            )

    def _invite_of(self, tenant_id):
        """The registry entry of the invite that produced this tenant, if any."""
        for entry in self.registry["invites"].values():
            if entry.get("usedBy") == tenant_id:
                return entry
        return None

    def _suspension_of(self, tenant_id):
        """When this tenant's authorization was revoked, if ever. The invite
        is the only place authorization state lives; suspension is derived,
        never stored on the tenant."""
        invite = self._invite_of(tenant_id)
        return (invite or {}).get("revokedAt")

    def revoke(self, code):
        """Kill an invite now instead of at its expiry (`revoke awi_...`).

        A pending code stops pairing on the spot; a used one suspends the
        tenant it produced — its token stops authenticating, its connections
        stop ticking, and its capacity slot frees, all derived from this one
        flag. Machine pairings are untouched: revoke is a pure authorization
        act, fully reversible with `restore`. Nothing is deleted — the
        ledger keeps every row.
        """
        with self._registry_transaction():
            entry = self.registry["invites"].get(_hash_secret(code))
            if entry is None:
                raise ApiError(404, f"no such invite: {code}\nfix: list codes with: awewarm-hub list invites --reveal")
            if entry.get("revokedAt"):
                raise ApiError(403, f"invite already revoked — restore it instead: awewarm-hub restore {code}")
            expires = schedule.parse_ts(entry.get("expiresAt"))
            now = datetime.now().astimezone()
            entry["revokedAt"] = schedule.iso(now)
            self._save()
        used_by = entry.get("usedBy")
        who = f" — {used_by} suspended" if used_by else ""
        self.log(f"invite revoked ({entry.get('note') or 'no note'}){who}")
        was_expired = expires is not None and expires <= now
        return {"ok": True, "status": "used" if used_by else ("expired" if was_expired else "pending"),
                "tenant": used_by, "note": entry.get("note")}

    def delete_invite(self, code):
        """Remove an invite row outright (`revoke --delete`) — no tombstone.

        A pending code simply never pairs again. A used one takes its tenant
        with it: authorization lives on the invite row alone, so leaving the
        tenant behind would resurrect it (auth would see no revokedAt and
        fall back to the global machine cap). The tenant's workspace stays
        on disk — the same rule as the v1→v2 migration. Unlike revoke this
        is not reversible: the row is gone, so `restore` has nothing to act
        on. Works on a revoked row too — that is the purge-a-tombstone case.
        """
        with self._registry_transaction():
            digest = _hash_secret(code)
            entry = self.registry["invites"].get(digest)
            if entry is None:
                raise ApiError(404, f"no such invite: {code}\nfix: list codes with: awewarm-hub list invites --reveal")
            used_by = entry.get("usedBy")
            del self.registry["invites"][digest]
            if used_by:
                self.registry["tenants"].pop(used_by, None)
                self.tenants.pop(used_by, None)
            self._save()
        who = f" — {used_by} removed" if used_by else ""
        self.log(f"invite deleted ({entry.get('note') or 'no note'}){who}")
        return {"ok": True, "tenant": used_by, "note": entry.get("note")}

    def restore(self, code):
        """Reverse `revoke`: a pending code pairs again; a used one brings
        its tenant back, capacity permitting."""
        with self._registry_transaction():
            entry = self.registry["invites"].get(_hash_secret(code))
            if entry is None:
                raise ApiError(404, f"no such invite: {code}\nfix: list codes with: awewarm-hub list invites --reveal")
            if not entry.get("revokedAt"):
                raise ApiError(403, "invite is not revoked — nothing to restore")
            used_by = entry.get("usedBy")
            if used_by and used_by in self.registry["tenants"]:
                self._require_capacity()
            entry.pop("revokedAt", None)
            self._save()
        self.log(f"invite restored ({entry.get('note') or 'no note'})")
        return {"ok": True, "tenant": used_by}

    def auth(self, bearer, machine=None):
        """Bearer token → tenant, behind the machine cap and rate-limit gate."""
        with self.lock:
            self._refresh()  # revocations happen in other processes (awewarm-hub revoke)
            digest = _hash_secret(bearer)
            tenant = next(
                (t for t in self.tenants.values() if hmac.compare_digest(t.token_hash, digest)),
                None,
            )
            if tenant is None:
                raise ApiError(
                    401,
                    "invalid hub token — re-pair with an invite, or reuse a saved token: awewarm remote connect <url> --token <saved>",
                )
            invite = self._invite_of(tenant.id)
            if invite and invite.get("revokedAt"):
                raise ApiError(
                    401,
                    "hub token suspended by the operator — ask them to restore your invite (awewarm-hub restore <code>)",
                )
            if not machine:
                raise ApiError(
                    403,
                    "this hub requires a machine id — update awewarm on this machine and reconnect",
                )
            # the cap lives on the authorization: per-invite override, else global
            cap = (invite or {}).get("machines") or self.max_machines
            with self._registry_transaction():
                if tenant.id not in self.tenants:
                    raise ApiError(401, "hub token was revoked during this request")
                machines = tenant.record.setdefault("machines", [])
                if machine not in machines:
                    if len(machines) >= cap:
                        raise ApiError(
                            403,
                            f"this token is already paired on {len(machines)} machine(s) "
                            f"(limit {cap}) — ask the operator for a new invite "
                            "or more machines on yours",
                        )
                    machines.append(machine)
                    self._save()
            now = time.monotonic()
            while tenant.requests and now - tenant.requests[0] >= 60:
                tenant.requests.popleft()
            if len(tenant.requests) >= HUB_RATE_PER_MINUTE:
                raise ApiError(429, "too many requests from this tenant — is a client looping?")
            tenant.requests.append(now)
            self._refresh_seen(tenant)
            return tenant

    def _refresh_seen(self, tenant):
        now = datetime.now().astimezone()
        seen = schedule.parse_ts(tenant.record.get("lastSeenAt"))
        if seen is not None and now - seen < HUB_SEEN_PRECISION:
            return
        with self._registry_transaction():
            if tenant.id not in self.tenants:
                raise ApiError(401, "hub token was revoked during this request")
            tenant.record["lastSeenAt"] = schedule.iso(now)
            self._save()

    # --- quotas and usage ---

    def check_conn_quota(self, tenant, conn_id):
        """Per-tenant connection cap; replacing an existing id never counts."""
        with tenant.warm.lock:
            conns = tenant.warm.config["connections"]
            if conn_id not in conns and len(conns) >= self.max_conns_per_tenant:
                raise ApiError(
                    403,
                    f"connection quota reached ({self.max_conns_per_tenant} per tenant on this hub)",
                )

    def _bump_usage(self, tenant, count):
        with self._registry_transaction():
            if tenant.id not in self.tenants:
                return
            today = datetime.now().astimezone().date().isoformat()
            usage = tenant.record.setdefault("usage", {})
            if usage.get("day") != today:
                usage["day"] = today
                usage["today"] = 0
            usage["today"] = usage.get("today", 0) + count
            usage["total"] = usage.get("total", 0) + count
            self._save()

    def summarize(self):
        """Rows for `list users` — no secrets by construction."""
        joined_with = {
            entry.get("usedBy"): entry.get("code")
            for entry in self.registry["invites"].values()
            if entry.get("usedBy")
        }
        rows = []
        for tenant_id in sorted(self.tenants):
            tenant = self.tenants[tenant_id]
            warm = tenant.warm
            connections = []
            for cid in sorted(warm.config["connections"]):
                conn = warm.config["connections"][cid]
                cs = conn_state(warm.state, cid)
                transport = conn.get("transport") or {}
                connections.append({
                    "id": cid,
                    "status": schedule.status_word(cid, conn, cs),
                    "mode": (conn.get("schedule") or {}).get("mode", "fixed"),
                    "api": transport.get("baseUrl"),
                    "protocol": transport.get("kind"),
                    "model": (conn.get("activation") or {}).get("model"),
                    "enabled": conn.get("enabled", True),
                    "timezone": conn.get("timezone"),
                    "nextDueAt": cs.get("nextDueAt"),
                })
            rows.append({
                "tenant": tenant_id,
                "note": tenant.note,
                "invite": joined_with.get(tenant_id),
                "suspended": bool(self._suspension_of(tenant_id)),
                "machines": len(tenant.record.get("machines") or []),
                "createdAt": tenant.record.get("createdAt"),
                "lastSeenAt": tenant.record.get("lastSeenAt"),
                "connections": connections,
                "usage": dict(tenant.record.get("usage") or {}),
            })
        return rows

    # --- the tick: every tenant's workspace in one pass ---

    def tick(self, now_fn=None):
        fired, held = 0, []
        with self._registry_transaction():
            tenants = [
                self.tenants[tenant_id] for tenant_id in sorted(self.tenants)
                if not self._suspension_of(tenant_id)
            ]
        for tenant in tenants:
            result = tenant.warm.tick(now_fn=now_fn)
            fired += result["fired"]
            held.extend(result["held"])
            if result["fired"]:
                self._bump_usage(tenant, result["fired"])
        return {"fired": fired, "held": held}

    def run_now(self, tenant, conn_id, reset_due=False, allow_auto_disabled=False):
        result = tenant.warm.run_now(
            conn_id, reset_due=reset_due, allow_auto_disabled=allow_auto_disabled
        )
        if result.get("ok"):
            self._bump_usage(tenant, 1)
        return result


def _hash_secret(value):
    return hashlib.sha256((value or "").encode()).hexdigest()


def make_hub_server(data_dir, bind="127.0.0.1", port=8790,
                    max_tenants=DEFAULT_MAX_TENANTS, max_conns_per_tenant=DEFAULT_MAX_CONNS_PER_TENANT,
                    max_machines=DEFAULT_MAX_MACHINES):
    """Build the Hub engine plus its HTTP server. Port 0 picks a free one."""
    engine = Hub(
        data_dir, max_tenants=max_tenants, max_conns_per_tenant=max_conns_per_tenant,
        max_machines=max_machines,
    )
    handler = type("BoundHandler", (HubHandler,), {"hub": engine})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    return engine, httpd


def _serve_forever(engine, httpd, tick_seconds):
    """API in the main thread, the tick loop beside it."""

    def _loop():
        while True:
            time.sleep(tick_seconds)
            try:
                engine.tick()
            except Exception as exc:  # the loop must outlive any single tick
                engine.log(f"tick crashed: {exc!r}")

    threading.Thread(target=_loop, daemon=True, name="awewarm-hub-tick").start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()


def run(data_dir, bind="127.0.0.1", port=8790, tick_seconds=60,
        max_tenants=DEFAULT_MAX_TENANTS, max_conns_per_tenant=DEFAULT_MAX_CONNS_PER_TENANT,
        max_machines=DEFAULT_MAX_MACHINES):
    """Serve forever: build the hub, announce it, then serve_forever."""
    engine, httpd = make_hub_server(
        data_dir, bind=bind, port=port,
        max_tenants=max_tenants, max_conns_per_tenant=max_conns_per_tenant,
        max_machines=max_machines,
    )
    actual = httpd.server_address[1]
    engine.record_launch(bind, actual)
    print(f"awewarm-hub serve {__version__} (engine awewarm {awewarm_version})")
    print(f"  data dir: {engine.data_dir}  (config/state/log — no secrets ever written to disk)")
    print(f"  listening: http://{bind}:{actual}")
    print(f"  hub mode: {len(engine.tenants)} of max {engine.max_tenants} tenants, "
          f"{engine.max_conns_per_tenant} connections each")
    print("  auth: per-tenant tokens (kept in tenants.json; recovery: list invites --token); pair by invite: awewarm-hub invite")
    print("  Ctrl-C stops the server")
    _serve_forever(engine, httpd, tick_seconds)
