"""awewarm-hub serve: many users behind one hub process.

Covers the invite→token pairing flow, tenant isolation (two users may both
delegate a connection named glm), quotas, the restart story (pairings persist
via hashed tokens while API keys stay RAM-only), revocation, and the client
`remote connect` flow (open-source awewarm) against a live hub.
"""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from helpers import IsolatedTestCase, plan_connection, start_http_server, stop_http_server

import awewarm_hub
from awewarm import remote as remote_client
from awewarm import schedule, transport
from awewarm import server as solo_server  # the single-tenant engine this package builds on
from awewarm.server import ApiError
from awewarm_hub import engine
from awewarm_hub.cli import cli as hub_cli
from awewarm.cli import cli as client_cli  # what hub users run

TZ = "Asia/Shanghai"
HUB_RUNNER = CliRunner()
CLIENT_RUNNER = CliRunner()


def invoke(*args, **kwargs):
    kwargs.setdefault("prog_name", "awewarm-hub")
    return HUB_RUNNER.invoke(hub_cli, *args, **kwargs)


def invoke_client(*args, **kwargs):
    kwargs.setdefault("prog_name", "awewarm")
    return CLIENT_RUNNER.invoke(client_cli, *args, **kwargs)


def at(hhmm, seconds=0):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 8, 20, hour, minute, seconds, tzinfo=ZoneInfo(TZ))


class HubCase(unittest.TestCase):
    """A real Hub on an ephemeral port, plus its URL."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "hub"
        self.make_hub()

    def make_hub(self, max_tenants=50, max_conns_per_tenant=5, max_machines=1):
        self.hub, self.httpd = engine.make_hub_server(
            self.data_dir, "127.0.0.1", 0,
            max_tenants=max_tenants, max_conns_per_tenant=max_conns_per_tenant,
            max_machines=max_machines,
        )
        self.server_thread = start_http_server(self.httpd)
        self.addCleanup(stop_http_server, self.httpd, self.server_thread)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def join(self, note=None):
        """The user flow: mint an invite, burn it for a personal token."""
        invite = self.hub.mint_invite(note)
        joined = remote_client.join(self.url, invite)
        return joined["token"], joined["tenantId"]

    def code_of(self, tenant_id):
        """The invite code a tenant joined with — the only handle for
        revoking its authorization."""
        return (self.hub._invite_of(tenant_id) or {}).get("code")

    def push_plan(self, token, conn_id="glm", fixed_at=("03:00",)):
        conn = plan_connection(fixed_at=fixed_at, days="every-day")
        return remote_client.push_connection(self.url, token, conn_id, conn, "sk-test", TZ)

    def registry(self):
        return json.loads((self.data_dir / "tenants.json").read_text())


class PairingTests(HubCase):
    def test_healthz_advertises_hub_mode(self):
        health = remote_client.healthz(self.url)
        self.assertTrue(health["ok"])
        self.assertTrue(health["hub"])
        self.assertTrue(health["claimed"])  # auth is per-token; nothing to pre-claim
        self.assertEqual(health["version"], awewarm_hub.__version__)

    def test_state_view_advertises_the_hub_version(self):
        token, _ = self.join("alice")
        view = remote_client.fetch_state(self.url, token)
        # the view's own "version" is the engine package's; the hub's rides along
        self.assertEqual(view["hubVersion"], awewarm_hub.__version__)
        self.assertEqual(view["version"], solo_server.__version__)

    def test_join_returns_a_working_token(self):
        token, tenant_id = self.join("alice")
        self.assertTrue(tenant_id.startswith("t_"))
        view = remote_client.fetch_state(self.url, token)
        self.assertEqual(view["connections"], {})
        self.assertEqual(view["tenant"], tenant_id)

    def test_invite_is_single_use(self):
        invite = self.hub.mint_invite("alice")
        remote_client.join(self.url, invite)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, invite)
        self.assertIn("403", str(ctx.exception))

    def test_expired_invite_is_refused_and_burned(self):
        invite = self.hub.mint_invite("alice")
        digest = engine._hash_secret(invite)
        self.hub.registry["invites"][digest]["expiresAt"] = schedule.iso(
            datetime.now().astimezone()
        )
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, invite)
        self.assertIn("expired", str(ctx.exception))
        self.assertNotIn(digest, self.registry()["invites"])

    def test_revoked_invite_no_longer_pairs(self):
        invite = self.hub.mint_invite("alice")
        self.hub.revoke(invite)
        digest = engine._hash_secret(invite)
        self.assertTrue(self.registry()["invites"][digest].get("revokedAt"))  # kept, flagged
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, invite)
        self.assertIn("revoked", str(ctx.exception))

    def test_restored_invite_pairs_again(self):
        invite = self.hub.mint_invite("alice")
        self.hub.revoke(invite)
        self.hub.restore(invite)
        joined = remote_client.join(self.url, invite)
        self.assertTrue(joined["token"].startswith("awt_"))

    def test_revoke_flags_an_expired_entry(self):
        invite = self.hub.mint_invite("alice")
        digest = engine._hash_secret(invite)
        self.hub.registry["invites"][digest]["expiresAt"] = schedule.iso(
            datetime.now().astimezone()
        )
        result = self.hub.revoke(invite)
        self.assertEqual(result["status"], "expired")
        self.assertTrue(self.registry()["invites"][digest].get("revokedAt"))

    def test_revoke_suspends_the_tenant_it_produced(self):
        invite = self.hub.mint_invite("alice")
        joined = remote_client.join(self.url, invite)
        result = self.hub.revoke(invite)
        self.assertEqual(result["status"], "used")
        self.assertEqual(result["tenant"], joined["tenantId"])
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, joined["token"])
        self.assertIn("suspended", str(ctx.exception))
        digest = engine._hash_secret(invite)
        self.assertIn(digest, self.registry()["invites"])  # the ledger row stays
        self.hub.restore(invite)  # the code is a handle for the same suspension
        self.assertEqual(remote_client.fetch_state(self.url, joined["token"])["connections"], {})

    def test_malformed_invite_is_a_400(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, "not-an-invite")
        self.assertIn("400", str(ctx.exception))

    def test_claim_flow_does_not_exist_on_a_hub(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.claim(self.url, "awt_" + "t" * 40)
        self.assertIn("invite", str(ctx.exception))

    def test_join_does_not_exist_on_a_single_tenant_server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        warm, httpd = solo_server.make_server(Path(tmp.name) / "single", "127.0.0.1", 0)
        thread = start_http_server(httpd)
        self.addCleanup(stop_http_server, httpd, thread)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(url, "awi_" + "i" * 30)
        self.assertIn("404", str(ctx.exception))

    def test_registry_keeps_the_hash_for_auth_and_the_token_for_recovery(self):
        token, tenant_id = self.join("alice")
        record = self.registry()["tenants"][tenant_id]
        self.assertEqual(len(record["tokenHash"]), 64)  # sha256 hex — what auth compares
        self.assertEqual(record["token"], token)  # in the clear for `list invites --token`
        self.assertFalse(list(self.data_dir.rglob("secrets.json")))  # API keys still never touch disk

    def test_tenant_cap(self):
        self.make_hub(max_tenants=1)
        self.join("alice")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.join("bob")
        self.assertIn("full", str(ctx.exception))


class IsolationTests(HubCase):
    def test_same_connection_id_in_two_tenants_stays_separate(self):
        alice, alice_id = self.join("alice")
        bob, bob_id = self.join("bob")
        self.push_plan(alice)
        self.push_plan(bob)
        alice_view = remote_client.fetch_state(self.url, alice)
        bob_view = remote_client.fetch_state(self.url, bob)
        self.assertEqual(list(alice_view["connections"]), ["glm"])
        self.assertEqual(list(bob_view["connections"]), ["glm"])
        self.assertEqual(alice_view["tenant"], alice_id)
        self.assertEqual(bob_view["tenant"], bob_id)
        # each workspace is its own directory; deleting one leaves the other
        remote_client.delete_connection(self.url, alice, "glm")
        self.assertEqual(remote_client.fetch_state(self.url, bob)["connections"]["glm"]["config"]["timezone"], TZ)

    def test_one_tenant_cannot_touch_another(self):
        alice, _ = self.join("alice")
        bob, _ = self.join("bob")
        self.push_plan(bob)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.delete_connection(self.url, alice, "glm")
        self.assertIn("404", str(ctx.exception))

    def test_connection_quota_per_tenant(self):
        self.make_hub(max_conns_per_tenant=1)
        alice, _ = self.join("alice")
        bob, _ = self.join("bob")
        self.push_plan(alice, "glm")
        self.push_plan(bob, "glm")  # quotas are per tenant, not global
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.push_plan(alice, "kimi")
        self.assertIn("quota", str(ctx.exception))
        self.push_plan(alice, "glm")  # replacing an existing id never counts


class LifecycleTests(HubCase):
    def test_pairing_survives_a_restart_keys_do_not(self):
        token, _ = self.join("alice")
        self.push_plan(token)
        stop_http_server(self.httpd, self.server_thread)
        self.make_hub()  # same data dir, fresh process
        view = remote_client.fetch_state(self.url, token)  # no re-claim needed
        self.assertTrue(view["connections"]["glm"]["keyMissing"])
        remote_client.push_keys(self.url, token, {"glm": "sk-test"})
        self.assertFalse(remote_client.fetch_state(self.url, token)["connections"]["glm"]["keyMissing"])

    def test_revoke_suspends_the_token_and_keeps_the_workspace(self):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        code = self.code_of(tenant_id)
        self.hub.revoke(code)
        self.assertTrue((self.data_dir / "tenants" / tenant_id).exists())  # kept on disk
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token)
        self.assertIn("suspended", str(ctx.exception))
        record = self.registry()["tenants"][tenant_id]
        self.assertNotIn("suspendedAt", record)  # suspension lives on the invite only
        self.assertTrue(self.registry()["invites"][engine._hash_secret(code)]["revokedAt"])

    def test_suspension_frees_the_ram_workspace_and_its_keys(self):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        tenant = self.hub.tenants[tenant_id]
        warm = tenant.warm  # materialized: the config/state mirror plus the keyring
        self.assertIn("glm", warm.keys)
        self.hub.revoke(self.code_of(tenant_id))
        self.assertIsNone(tenant._warm)  # the mirror and the keys left RAM
        self.assertTrue((self.data_dir / "tenants" / tenant_id).exists())  # files stay
        self.hub.restore(self.code_of(tenant_id))
        view = remote_client.fetch_state(self.url, token)
        self.assertTrue(view["connections"]["glm"]["keyMissing"])  # keys died with the workspace
        remote_client.push_keys(self.url, token, {"glm": "sk-test"})
        self.assertFalse(remote_client.fetch_state(self.url, token)["connections"]["glm"]["keyMissing"])

    def test_restore_brings_the_token_back(self):
        token, tenant_id = self.join("alice")
        code = self.code_of(tenant_id)
        self.hub.revoke(code)
        self.hub.restore(code)
        self.assertEqual(remote_client.fetch_state(self.url, token)["connections"], {})
        self.assertFalse(self.registry()["invites"][engine._hash_secret(code)].get("revokedAt"))

    def test_delete_removes_the_tenant_keeps_the_workspace(self):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        code = self.code_of(tenant_id)
        self.hub.delete_invite(code)
        registry = self.registry()
        self.assertNotIn(engine._hash_secret(code), registry["invites"])  # no tombstone
        self.assertNotIn(tenant_id, registry["tenants"])  # the tenant goes with its invite
        self.assertTrue((self.data_dir / "tenants" / tenant_id).exists())  # workspace kept on disk
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token)
        self.assertIn("invalid hub token", str(ctx.exception))
        with self.assertRaises(ApiError) as ctx:  # the row is gone — nothing to restore
            self.hub.restore(code)
        self.assertIn("no such invite", str(ctx.exception))

    def test_release_keeps_the_pairing(self):
        token, _ = self.join("alice")
        result = remote_client.release(self.url, token)
        self.assertFalse(result["released"])  # capacity is the operator's call
        self.assertEqual(remote_client.fetch_state(self.url, token)["connections"], {})

    def test_rate_limit_blocks_a_looping_client(self):
        token, _ = self.join("alice")
        for _ in range(engine.HUB_RATE_PER_MINUTE):
            remote_client.fetch_state(self.url, token)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token)
        self.assertIn("429", str(ctx.exception))


class MachineTests(HubCase):
    """One token, N machines. The cap is a property of the authorization:
    stamped onto the invite at minting (`invite --machines`), defaulting to
    the serve flag; the cap rides on a per-install machine id sent with
    every authed request."""

    def test_a_token_serves_one_machine_by_default(self):
        token, _ = self.join("alice")
        remote_client.fetch_state(self.url, token, machine="awm_desktop")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token, machine="awm_laptop")
        self.assertIn("already paired on 1 machine", str(ctx.exception))

    def test_the_same_machine_reconnects_freely(self):
        token, _ = self.join("alice")
        for _ in range(3):
            remote_client.fetch_state(self.url, token, machine="awm_desktop")
        machines = next(iter(self.registry()["tenants"].values()))["machines"]
        self.assertEqual(machines, ["awm_desktop"])

    def test_join_seeds_the_joining_machine(self):
        code = self.hub.mint_invite("alice")
        remote_client.join(self.url, code, machine="awm_desktop")
        machines = next(iter(self.registry()["tenants"].values()))["machines"]
        self.assertEqual(machines, ["awm_desktop"])

    def test_a_headerless_client_is_refused(self):
        token, _ = self.join("alice")
        request = urllib.request.Request(
            self.url + "/v1/state", method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        self.assertIn(b"machine id", ctx.exception.read())

    def test_max_machines_can_be_raised(self):
        self.make_hub(max_machines=2)
        token, _ = self.join("alice")
        remote_client.fetch_state(self.url, token, machine="awm_desktop")
        remote_client.fetch_state(self.url, token, machine="awm_laptop")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token, machine="awm_phone")
        self.assertIn("limit 2", str(ctx.exception))

    def test_revoke_and_restore_keep_the_machines(self):
        token, tenant_id = self.join("alice")
        code = self.code_of(tenant_id)
        remote_client.fetch_state(self.url, token, machine="awm_desktop")
        self.hub.revoke(code)
        self.hub.restore(code)
        # revoke is a pure authorization act — the pairing survives it intact
        remote_client.fetch_state(self.url, token, machine="awm_desktop")
        self.assertEqual(self.registry()["tenants"][tenant_id]["machines"], ["awm_desktop"])

    def test_an_invite_can_carry_a_higher_machine_cap(self):
        code = self.hub.mint_invite("alice", machines=3)
        joined = remote_client.join(self.url, code)
        for machine in ("awm_desktop", "awm_laptop", "awm_phone"):
            remote_client.fetch_state(self.url, joined["token"], machine=machine)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, joined["token"], machine="awm_tablet")
        self.assertIn("limit 3", str(ctx.exception))

    def test_a_per_invite_cap_beats_the_global_one(self):
        self.make_hub(max_machines=2)
        raised = self.hub.mint_invite("alice", machines=1)  # tighter than the global cap
        joined = remote_client.join(self.url, raised)
        remote_client.fetch_state(self.url, joined["token"], machine="awm_desktop")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, joined["token"], machine="awm_laptop")
        self.assertIn("limit 1", str(ctx.exception))

    def test_an_invite_without_a_stamped_cap_follows_the_global_one(self):
        code = self.hub.mint_invite("alice")
        digest = engine._hash_secret(code)
        self.hub.registry["invites"][digest].pop("machines")  # minted before caps moved onto codes
        self.hub._save()
        self.make_hub(max_machines=2)  # raise the global cap afterwards
        joined = remote_client.join(self.url, code)
        remote_client.fetch_state(self.url, joined["token"], machine="awm_desktop")
        remote_client.fetch_state(self.url, joined["token"], machine="awm_laptop")


class SeenStampTests(HubCase):
    """LAST SEEN: stamped in RAM per request, persisted once per tick.

    A request must never rewrite tenants.json — at N active tenants the old
    per-tenant writes cost N full-registry writes per window; the flush
    collapses any number of stamps into one."""

    def test_a_request_stamps_ram_but_never_writes_the_registry(self):
        token, tenant_id = self.join("alice")
        remote_client.fetch_state(self.url, token)  # first contact pairs the machine
        before = self.registry()["tenants"][tenant_id]
        remote_client.fetch_state(self.url, token)
        self.assertIsNotNone(self.hub.tenants[tenant_id].pending_seen)
        self.assertEqual(self.registry()["tenants"][tenant_id], before)

    def test_tick_flushes_every_pending_stamp_in_one_write(self):
        alice, alice_id = self.join("alice")
        bob, bob_id = self.join("bob")
        self.push_plan(alice)
        self.push_plan(bob)
        remote_client.fetch_state(self.url, alice)
        remote_client.fetch_state(self.url, bob)
        with mock.patch("awewarm_hub.engine._write_json", wraps=engine._write_json) as write:
            self.hub.tick(now_fn=lambda conn: at("00:00"))  # nothing due; flush only
        write.assert_called_once()
        for tenant_id in (alice_id, bob_id):
            self.assertIsNone(self.hub.tenants[tenant_id].pending_seen)
            self.assertTrue(self.registry()["tenants"][tenant_id]["lastSeenAt"])

    def test_a_crash_loses_at_most_the_pending_stamps(self):
        token, tenant_id = self.join("alice")
        remote_client.fetch_state(self.url, token)  # stamped, never flushed
        fresh = engine.Hub(self.data_dir)  # what a restart reconstructs
        self.assertIsNone(fresh.registry["tenants"][tenant_id].get("lastSeenAt"))


class CrossProcessTests(HubCase):
    """A serve that outlives operator commands: invites minted and tenants
    revoked by separate one-shot processes (the hub CLI) must be honored
    without a restart."""

    def test_join_honors_an_invite_minted_by_another_process(self):
        operator = engine.Hub(self.data_dir)  # what `awewarm-hub invite` runs as
        code = operator.mint_invite("alice")
        joined = remote_client.join(self.url, code)  # against the long-lived serve
        self.assertTrue(joined["token"].startswith("awt_"))

    def test_caps_raised_by_another_process_apply_without_a_restart(self):
        self.make_hub(max_tenants=1)
        self.join("alice")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.join("bob")
        self.assertIn("hub is full (1 active tenants)", str(ctx.exception))
        engine.Hub(self.data_dir).set_caps(max_tenants=2)  # `awewarm-hub config --max-tenants 2`
        self.join("bob")  # the live serve admits the second tenant — no restart

    def test_conn_quota_raised_by_another_process_applies_without_a_restart(self):
        self.make_hub(max_conns_per_tenant=1)
        token, _ = self.join("alice")
        self.push_plan(token, "glm")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.push_plan(token, "kimi")
        self.assertIn("quota reached (1 per tenant", str(ctx.exception))
        engine.Hub(self.data_dir).set_caps(max_conns_per_tenant=2)  # `awewarm-hub config --max-conns-per-tenant 2`
        self.push_plan(token, "kimi")  # the second connection pushes through on the live serve

    def test_revoked_token_stops_working_without_a_restart(self):
        token, tenant_id = self.join("alice")
        engine.Hub(self.data_dir).revoke(self.code_of(tenant_id))  # `awewarm-hub revoke` in another process
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token)
        self.assertIn("401", str(ctx.exception))

    def test_a_revoke_by_another_process_frees_the_serve_workspace(self):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        tenant = self.hub.tenants[tenant_id]
        engine.Hub(self.data_dir).revoke(self.code_of(tenant_id))
        with self.assertRaises(remote_client.RemoteError):
            remote_client.fetch_state(self.url, token)  # the 401 path itself refreshes the registry
        self.assertIsNone(tenant._warm)

    def test_revoked_invite_stops_pairing_without_a_restart(self):
        code = engine.Hub(self.data_dir).mint_invite("alice")  # `awewarm-hub invite`
        engine.Hub(self.data_dir).revoke(code)  # `awewarm-hub revoke awi_...`
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, code)  # against the long-lived serve
        self.assertIn("403", str(ctx.exception))

    def test_stale_usage_write_cannot_lift_a_suspension(self):
        token, tenant_id = self.join("alice")
        stale_tenant = self.hub.tenants[tenant_id]
        code = self.code_of(tenant_id)
        engine.Hub(self.data_dir).revoke(code)

        self.hub._bump_usage(stale_tenant, 1)

        invite = engine.Hub(self.data_dir).registry["invites"][engine._hash_secret(code)]
        self.assertTrue(invite.get("revokedAt"))  # the bump must not clear it
        with self.assertRaises(ApiError) as ctx:
            self.hub.auth(token)
        self.assertEqual(ctx.exception.status, 401)

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_tick_does_not_fire_a_suspended_tenant(self, _send):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        workspace = self.data_dir / "tenants" / tenant_id
        engine.Hub(self.data_dir).revoke(self.code_of(tenant_id))

        result = self.hub.tick(now_fn=lambda conn: at("03:00", seconds=30))

        self.assertEqual(result["fired"], 0)
        self.assertTrue(workspace.exists())  # suspension keeps the workspace

    def test_suspension_frees_a_slot_and_restore_retakes_it(self):
        self.make_hub(max_tenants=1)
        token, tenant_id = self.join("alice")
        code = self.code_of(tenant_id)
        engine.Hub(self.data_dir).revoke(code)  # frees the only slot
        bob_token, _ = self.join("bob")  # joins while alice is suspended
        self.assertTrue(bob_token.startswith("awt_"))
        with self.assertRaises(ApiError) as ctx:
            self.hub.restore(code)  # no room left for alice
        self.assertIn("full", str(ctx.exception))

    def test_reload_uses_disk_record_but_keeps_runtime_tenant_state(self):
        token, tenant_id = self.join("alice")
        tenant = self.hub.tenants[tenant_id]
        tenant.record.setdefault("usage", {})["total"] = 7  # deliberately unsaved stale memory
        tenant.requests.append(123)
        code = engine.Hub(self.data_dir).mint_invite("bob")  # operator writes tenants.json
        self.hub._refresh()
        # Disk is authoritative for persisted records; runtime-only state stays on the Tenant.
        self.assertIn(engine._hash_secret(code), self.hub.registry["invites"])
        self.assertEqual(self.hub.registry["tenants"][tenant_id]["usage"]["total"], 0)
        self.assertIs(self.hub.tenants[tenant_id], tenant)
        self.assertEqual(list(tenant.requests), [123])


class MigrationTests(unittest.TestCase):
    """v1→v2, once, at load: the invite becomes the only authorization
    ledger. Crafted by hand-editing tenants.json back into v1 shapes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "hub"
        self.path = self.data_dir / "tenants.json"
        self.hub = engine.Hub(self.data_dir)

    def rewind(self):
        """Freeze the on-disk registry into a v1 shape for one invite."""
        data = json.loads(self.path.read_text())
        data["version"] = 1
        self.path.write_text(json.dumps(data))

    def test_suspension_moves_from_the_tenant_to_its_invite(self):
        code = self.hub.mint_invite("alice")
        self.hub.join(code)
        self.hub.registry["tenants"]  # loaded
        tenant_id = next(iter(self.hub.registry["tenants"]))
        digest = engine._hash_secret(code)
        self.hub.registry["tenants"][tenant_id]["suspendedAt"] = "2026-08-01T10:00:00+08:00"
        self.hub.registry["invites"][digest].pop("revokedAt", None)
        self.hub._save()
        self.rewind()

        migrated = engine.Hub(self.data_dir)
        record = migrated.registry["tenants"][tenant_id]
        self.assertNotIn("suspendedAt", record)
        self.assertEqual(
            migrated.registry["invites"][digest]["revokedAt"], "2026-08-01T10:00:00+08:00"
        )
        self.assertTrue(migrated._suspension_of(tenant_id))
        self.assertEqual(migrated.registry["version"], 2)

    def test_a_codeless_invite_row_takes_its_tenant_with_it(self):
        code = self.hub.mint_invite("alice")
        self.hub.join(code)
        tenant_id = next(iter(self.hub.registry["tenants"]))
        workspace = self.data_dir / "tenants" / tenant_id
        self.hub.tenants[tenant_id].warm  # materialize the workspace directory
        digest = engine._hash_secret(code)
        self.hub.registry["invites"][digest].pop("code")  # pre-"codes on disk" vintage
        self.hub._save()
        self.rewind()

        migrated = engine.Hub(self.data_dir)
        self.assertNotIn(digest, migrated.registry["invites"])  # unaddressable → gone
        self.assertNotIn(tenant_id, migrated.registry["tenants"])  # its token died with it
        self.assertTrue(workspace.exists())  # files stay on disk

    def test_a_codeless_pending_invite_row_just_goes(self):
        code = self.hub.mint_invite("alice")
        digest = engine._hash_secret(code)
        self.hub.registry["invites"][digest].pop("code")
        self.hub._save()
        self.rewind()

        migrated = engine.Hub(self.data_dir)
        self.assertEqual(migrated.registry["invites"], {})
        self.assertEqual(migrated.registry["tenants"], {})

    def test_migration_runs_once_and_leaves_v2_data_alone(self):
        code = self.hub.mint_invite("alice")
        self.hub.join(code)
        tenant_id = next(iter(self.hub.registry["tenants"]))
        mtime = self.path.stat().st_mtime_ns

        again = engine.Hub(self.data_dir)  # v2 on disk — no rewrite

        self.assertEqual(again.registry["version"], 2)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertIn(tenant_id, again.registry["tenants"])


class PersistKeysTests(HubCase):
    """The owner-opt-in key storage, hub side: off unless the operator allows
    it, on only for connections whose user confirmed, purged the moment either
    side withdraws (switch off, revoke, delete)."""

    def push_persist(self, token, conn_id="glm"):
        conn = plan_connection(fixed_at=("03:00",), days="every-day")
        return remote_client.push_connection(
            self.url, token, conn_id, conn, "sk-test", TZ, persist=True,
        )

    def keys_file(self, tenant_id):
        return self.data_dir / "tenants" / tenant_id / "keys.json"

    def test_default_off_refuses_persisted_pushes(self):
        token, tenant_id = self.join("alice")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.push_persist(token)
        self.assertIn("403", str(ctx.exception))
        self.assertIn("awewarm-hub config --persist-keys on", str(ctx.exception))
        self.assertFalse(self.keys_file(tenant_id).exists())

    def test_plain_pushes_still_work_with_storage_off(self):
        token, _ = self.join("alice")
        self.push_plan(token)  # RAM-only, the default, unaffected

    def test_operator_allow_plus_user_optin_persists_the_key(self):
        self.hub.set_persist_keys(True)  # `awewarm-hub config --persist-keys on`
        token, tenant_id = self.join("alice")
        self.push_persist(token)
        keys = self.keys_file(tenant_id)
        self.assertTrue(keys.exists())
        self.assertEqual(json.loads(keys.read_text()), {"glm": "sk-test"})
        import stat as stat_module
        if os.name != "nt":  # NTFS has no POSIX mode bits; chmod is advisory only.
            self.assertEqual(stat_module.S_IMODE(keys.stat().st_mode), 0o600)
        view = remote_client.fetch_state(self.url, token)
        self.assertTrue(view["connections"]["glm"]["keyPersisted"])

    def test_persisted_key_survives_a_serve_restart(self):
        self.hub.set_persist_keys(True)
        token, tenant_id = self.join("alice")
        self.push_persist(token)
        revived = engine.Hub(self.data_dir)  # what a restarted serve loads
        self.assertTrue(revived.persist_keys)  # the setting rides in the registry
        warm = revived.tenants[tenant_id].warm
        self.assertEqual(warm.missing_keys(), [])  # ticking without a re-push

    def test_switching_off_purges_every_stored_key(self):
        self.hub.set_persist_keys(True)
        token, tenant_id = self.join("alice")
        self.push_persist(token)
        self.hub.set_persist_keys(False)  # "no keys on my disk", said and done
        self.assertFalse(self.keys_file(tenant_id).exists())
        with self.assertRaises(remote_client.RemoteError):
            self.push_persist(token)

    def test_switch_flip_adopted_by_a_running_serve(self):
        token, tenant_id = self.join("alice")
        operator = engine.Hub(self.data_dir)  # a separate `config` process
        operator.set_persist_keys(True)
        self.push_persist(token)
        operator.set_persist_keys(False)
        self.hub._refresh()  # what every tenant action and tick do
        self.assertFalse(self.hub.persist_keys)
        self.assertFalse(self.keys_file(tenant_id).exists())
        self.assertEqual(self.hub.tenants[tenant_id].warm.missing_keys(), [])  # RAM keeps ticking

    def test_revoking_an_invite_purges_its_tenants_keys(self):
        self.hub.set_persist_keys(True)
        token, tenant_id = self.join("alice")
        self.push_persist(token)
        self.assertTrue(self.keys_file(tenant_id).exists())
        self.hub.revoke(self.code_of(tenant_id))
        self.assertFalse(self.keys_file(tenant_id).exists())

    def test_deleting_an_invite_purges_its_tenants_keys(self):
        self.hub.set_persist_keys(True)
        token, tenant_id = self.join("alice")
        self.push_persist(token)
        self.hub.delete_invite(self.code_of(tenant_id))
        self.assertFalse(self.keys_file(tenant_id).exists())

    def test_revoke_in_another_process_purges_via_refresh(self):
        self.hub.set_persist_keys(True)
        token, tenant_id = self.join("alice")
        self.push_persist(token)
        engine.Hub(self.data_dir).revoke(self.code_of(tenant_id))  # the CLI process
        self.hub._refresh()  # the live serve adopts the revocation
        self.assertFalse(self.keys_file(tenant_id).exists())

    def test_record_launch_carries_the_setting_across_restarts(self):
        operator = engine.Hub(self.data_dir)
        operator.set_persist_keys(True)
        relaunched = engine.Hub(self.data_dir)
        relaunched.record_launch("127.0.0.1", 8790)  # serve restarts, restamping
        self.assertTrue((self.data_dir / "tenants.json").exists())
        fresh = engine.Hub(self.data_dir)
        self.assertTrue(fresh.persist_keys)  # the operator's choice outlived it


class UsageTests(HubCase):
    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_tick_counts_activations_per_tenant(self, send):
        alice, alice_id = self.join("alice")
        bob, bob_id = self.join("bob")
        self.push_plan(alice)
        self.push_plan(bob, fixed_at=("04:00",))
        result = self.hub.tick(now_fn=lambda conn: at("03:00", seconds=30))
        self.assertEqual(result["fired"], 1)  # only alice's 03:00 slot was due
        usage = self.registry()["tenants"][alice_id]["usage"]
        self.assertEqual(usage["today"], 1)
        self.assertEqual(usage["total"], 1)
        self.assertEqual(self.registry()["tenants"][bob_id]["usage"]["total"], 0)

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_manual_run_counts_as_usage(self, send):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        self.assertTrue(remote_client.run_connection(self.url, token, "glm", allow_auto_disabled=True)["ok"])
        self.assertEqual(self.registry()["tenants"][tenant_id]["usage"]["total"], 1)


class HubCliTests(IsolatedTestCase):
    """The operator side: awewarm-hub invite / list / revoke against a data dir."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = str(Path(tmp.name) / "hub")
        self.dir_opt = ["--data-dir", self.data_dir]

    def test_invite_mints_one_use_code(self):
        result = invoke(["invite"] + self.dir_opt + ["--name", "alice"])
        self.assertEqual(result.exit_code, 0)
        code = next(line.strip() for line in result.output.splitlines() if line.strip().startswith("awi_"))
        self.assertTrue(engine.INVITE_RE.match(code))
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertIn(engine._hash_secret(code), registry["invites"])
        self.assertEqual(registry["invites"][engine._hash_secret(code)]["note"], "alice")
        self.assertEqual(registry["invites"][engine._hash_secret(code)]["code"], code)

    def test_invite_reports_a_busy_registry_without_a_traceback(self):
        busy = ApiError(503, "hub registry is busy — retry this request")
        with mock.patch.object(engine.Hub, "mint_invites", side_effect=busy):
            result = invoke(["invite"] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not mint the invite", result.output)
        self.assertIn("hub registry is busy", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_invite_tells_the_operator_the_user_command(self):
        result = invoke(["invite"] + self.dir_opt + ["--name", "alice"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("awewarm remote connect <hub-url> --invite awi_", result.output)

    def test_invite_count_mints_codes_sharing_name_expiry_and_cap(self):
        result = invoke(["invite"] + self.dir_opt + ["--name", "team", "--count", "3", "--machines", "2"])
        self.assertEqual(result.exit_code, 0)
        codes = [line.strip() for line in result.output.splitlines() if line.strip().startswith("awi_")]
        self.assertEqual(len(codes), 3)
        self.assertEqual(len(set(codes)), 3)
        self.assertIn("Each user runs", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertEqual(len(registry["invites"]), 3)
        for entry in registry["invites"].values():
            self.assertEqual(entry["note"], "team")
            self.assertEqual(entry["machines"], 2)
        self.assertEqual(len({entry["expiresAt"] for entry in registry["invites"].values()}), 1)

    def test_invite_count_stays_between_1_and_100(self):
        for bad in ("0", "101"):
            result = invoke(["invite"] + self.dir_opt + ["--count", bad])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("between 1 and 100", result.output)

    def test_invite_expires_in_sets_the_expiry(self):
        before = datetime.now().astimezone()
        for raw, delta in (("7d", timedelta(days=7)), ("30m", timedelta(minutes=30))):
            result = invoke(["invite"] + self.dir_opt + ["--expires-in", raw])
            self.assertEqual(result.exit_code, 0)
            self.assertIn(f"expires in {raw}", result.output)
            registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
            entry = next(iter(registry["invites"].values()))
            expires = schedule.parse_ts(entry["expiresAt"])
            self.assertGreaterEqual(expires, before + delta)
            self.assertLessEqual(expires, datetime.now().astimezone() + delta)
            registry["invites"].clear()  # the loop asserts per-mint expiry
            Path(self.data_dir, "tenants.json").write_text(json.dumps(registry))

    def test_invite_expires_in_rejects_bad_spellings(self):
        for bad in ("7", "1w", "48 h"):
            result = invoke(["invite"] + self.dir_opt + ["--expires-in", bad])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("must look like 90s, 30m, 12h or 7d", result.output)
        result = invoke(["invite"] + self.dir_opt + ["--expires-in", "0h"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("greater than 0", result.output)

    def test_list_shows_tenants_and_totals(self):
        engine_hub = engine.Hub(self.data_dir)
        invite = engine_hub.mint_invite("alice")
        engine_hub.join(invite)  # a tenant with no connections yet
        result = invoke(["list", "users"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("alice", result.output)
        self.assertIn("TENANT", result.output)
        self.assertIn("LAST SEEN", result.output)
        self.assertNotIn("https://", result.output)  # the API table needs --api

    def test_list_api_shows_each_connection_endpoint(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        engine_hub.tenants[joined["tenantId"]].warm.put_connection("glm", {
            "connection": plan_connection(), "apiKey": "sk-test", "timezone": TZ,
        })
        result = invoke(["list", "users"] + self.dir_opt + ["--api"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("API", result.output)
        self.assertIn("glm", result.output)
        self.assertIn("https://open.bigmodel.cn/api/anthropic", result.output)
        self.assertIn("anthropic-messages", result.output)
        self.assertIn("connected", result.output)

    def test_list_json_is_redacted(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        result = invoke(["list", "users"] + self.dir_opt + ["--json"])
        self.assertEqual(result.exit_code, 0)
        rows = json.loads(result.output)
        self.assertEqual(rows[0]["note"], "alice")
        self.assertNotIn(joined["token"], result.output)

    def test_list_users_masks_the_joining_code_by_default(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.join(code)
        result = invoke(["list", "users"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("INVITE", result.output)
        self.assertNotIn(code, result.output)
        self.assertIn(code[:8], result.output)
        self.assertIn("masked", result.output)

    def test_list_users_reveal_shows_the_joining_code(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.join(code)
        result = invoke(["list", "users"] + self.dir_opt + ["--reveal"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn(code, result.output)
        self.assertNotIn("masked", result.output)

    def test_list_users_json_follows_reveal(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.join(code)
        masked = invoke(["list", "users"] + self.dir_opt + ["--json"])
        self.assertNotIn(code, masked.output)
        revealed = invoke(["list", "users"] + self.dir_opt + ["--json", "--reveal"])
        rows = json.loads(revealed.output)
        self.assertEqual(rows[0]["invite"], code)

    def test_list_users_shows_a_dash_when_the_code_was_never_stored(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        path = Path(self.data_dir, "tenants.json")
        registry = json.loads(path.read_text())
        registry["invites"][engine._hash_secret(code)].pop("code")  # older versions never stored it
        path.write_text(json.dumps(registry))
        engine_hub = engine.Hub(self.data_dir)  # re-read the edited registry
        engine_hub.join(code)
        result = invoke(["list", "users"] + self.dir_opt + ["--reveal"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("Traceback", result.output)
        self.assertIn("—", result.output)

    def paired_hub_with_connection(self, note="alice", conn_id="glm"):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite(note))
        engine_hub.tenants[joined["tenantId"]].warm.put_connection(conn_id, {
            "connection": plan_connection(), "apiKey": "sk-test", "timezone": TZ,
        })
        return engine_hub, joined

    def test_status_shows_capacity_invites_and_tenants(self):
        engine_hub, joined = self.paired_hub_with_connection()
        engine_hub.record_launch("127.0.0.1", 1)  # nothing listens on port 1
        result = invoke(["status"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("tenants: 1/10 active", result.output)
        self.assertIn("connections: 1 delegated (max 5 per tenant)", result.output)
        self.assertIn("invites: 1 used", result.output)
        self.assertIn("NOT reachable", result.output)
        self.assertIn("alice", result.output)
        self.assertIn("awewarm-hub.log", result.output)
        self.assertIn("per-connection detail: awewarm-hub status --details", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertEqual(registry["serve"]["maxTenants"], 10)  # caps stamped for other processes

    def test_status_details_lists_each_connection(self):
        engine_hub, _ = self.paired_hub_with_connection()
        engine_hub.record_launch("127.0.0.1", 1)
        result = invoke(["status"] + self.dir_opt + ["--details"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("CONNECTION", result.output)
        self.assertIn("glm", result.output)
        self.assertIn("MODE", result.output)
        self.assertIn(TZ, result.output)
        self.assertNotIn("per-connection detail", result.output)  # the hint steps aside

    def stamp_seen(self, engine_hub, tenant_id, iso):
        with engine_hub._registry_transaction():
            engine_hub.tenants[tenant_id].record["lastSeenAt"] = iso
            engine_hub._save()

    def test_last_seen_renders_in_the_tenants_timezone(self):
        engine_hub, joined = self.paired_hub_with_connection()
        self.stamp_seen(engine_hub, joined["tenantId"], "2026-08-20T09:16:00+00:00")
        for args in (["status"], ["list", "users"]):
            result = invoke(args + self.dir_opt)
            self.assertEqual(result.exit_code, 0)
            # a UTC box, a Shanghai tenant: what their own status footer shows
            self.assertIn("2026-08-20 17:16 (+08)", result.output)

    def test_last_seen_without_connections_falls_back_to_the_box_zone(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        self.stamp_seen(engine_hub, joined["tenantId"], "2026-08-20T09:16:00+00:00")
        result = invoke(["list", "users"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertRegex(result.output, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \([+-]\d{2}(:\d{2})?\)")

    def test_status_without_a_launch_record_shows_unknown_caps(self):
        self.paired_hub_with_connection()
        result = invoke(["status"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("caps unknown", result.output)
        self.assertIn("never launched", result.output)

    def test_cli_hub_adopts_the_recorded_caps(self):
        engine.Hub(self.data_dir, max_tenants=3, max_conns_per_tenant=2).record_launch("127.0.0.1", 1)
        fresh = engine.Hub(self.data_dir)  # what a one-shot hub CLI process constructs
        self.assertEqual(fresh.max_tenants, 3)
        self.assertEqual(fresh.max_conns_per_tenant, 2)

    def test_status_reports_a_reachable_serve(self):
        hub_engine, httpd = engine.make_hub_server(self.data_dir, "127.0.0.1", 0)
        thread = start_http_server(httpd)
        self.addCleanup(stop_http_server, httpd, thread)
        hub_engine.record_launch("127.0.0.1", httpd.server_address[1])
        self.paired_hub_with_connection()
        result = invoke(["status"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("reachable", result.output)
        self.assertIn(f"v{awewarm_hub.__version__}, hub", result.output)

    def test_revoke_by_tenant_id_is_refused_with_guidance(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        result = invoke(["invite", "revoke", joined["tenantId"]] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer addressable", result.output)
        self.assertIn("hub list invites --reveal", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertNotIn("suspendedAt", registry["tenants"][joined["tenantId"]])  # untouched

    def test_revoke_by_code_suspends_the_tenant(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        result = invoke(["invite", "revoke", code] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("suspended", result.output)
        self.assertIn(joined["tenantId"], result.output)
        self.assertIn("awewarm-hub invite restore", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        record = registry["tenants"][joined["tenantId"]]
        self.assertNotIn("suspendedAt", record)  # nothing deleted, nothing mirrored
        self.assertTrue(registry["invites"][engine._hash_secret(code)]["revokedAt"])

    def test_revoke_reports_a_busy_registry_without_a_traceback(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        busy = ApiError(503, "hub registry is busy — retry this request")
        with mock.patch.object(engine.Hub, "revoke", side_effect=busy):
            result = invoke(["invite", "revoke", code] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not revoke the invite", result.output)
        self.assertIn("hub registry is busy", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_revoke_kills_a_pending_invite(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["invite", "revoke", code] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("invite revoked for alice", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertTrue(registry["invites"][engine._hash_secret(code)].get("revokedAt"))
        with self.assertRaises(ApiError):
            engine_hub.join(code)

    def test_revoke_aborts_without_confirmation(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["invite", "revoke", code] + self.dir_opt, input="n\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("aborted", result.output)
        self.assertTrue(engine_hub.join(code)["token"].startswith("awt_"))

    def test_delete_wipes_a_pending_invite_row(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["invite", "revoke", code, "--delete"] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("invite deleted for alice", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertNotIn(engine._hash_secret(code), registry["invites"])  # no revoked tombstone
        with self.assertRaises(ApiError):
            engine_hub.join(code)

    def test_delete_takes_the_used_invites_tenant_with_it(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        result = invoke(["invite", "revoke", code, "--delete"] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn(joined["tenantId"], result.output)
        self.assertIn("workspace stays on disk", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertNotIn(engine._hash_secret(code), registry["invites"])
        self.assertNotIn(joined["tenantId"], registry["tenants"])  # token dead, slot freed

    def test_delete_can_purge_an_already_revoked_row(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        engine_hub.revoke(code)
        result = invoke(["invite", "revoke", code, "--delete"] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)  # unlike plain revoke, a revoked row is deletable
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertNotIn(engine._hash_secret(code), registry["invites"])
        self.assertNotIn(joined["tenantId"], registry["tenants"])

    def test_delete_aborts_without_confirmation(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["invite", "revoke", code, "--delete"] + self.dir_opt, input="n\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("aborted", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertIn(engine._hash_secret(code), registry["invites"])

    def test_rename_relabels_a_pending_invite(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["invite", "rename", code, "team-a"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("invite renamed to team-a", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertEqual(registry["invites"][engine._hash_secret(code)]["note"], "team-a")
        listed = invoke(["list", "invites", "--reveal"] + self.dir_opt)
        self.assertIn("team-a", listed.output)

    def test_rename_takes_the_used_invites_tenant_with_it(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        result = invoke(["invite", "rename", code, "bob"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn(joined["tenantId"], result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertEqual(registry["invites"][engine._hash_secret(code)]["note"], "bob")
        self.assertEqual(registry["tenants"][joined["tenantId"]]["note"], "bob")  # list users follows
        users = invoke(["list", "users"] + self.dir_opt)
        self.assertIn("bob", users.output)

    def test_rename_rejects_unknown_codes_and_bad_names(self):
        engine_hub = engine.Hub(self.data_dir)
        engine_hub.mint_invite("alice")
        result = invoke(["invite", "rename", "awi_" + "x" * 20, "bob"] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no such invite", result.output)
        code = engine_hub.list_invites()[0]["code"]
        result = invoke(["invite", "rename", code, " "] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("single non-empty line", result.output)

    def test_invite_group_inherits_the_data_dir(self):
        # `invite --data-dir X revoke ...` — the group's dir reaches subcommands
        # that don't carry their own --data-dir
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["invite", "--data-dir", self.data_dir, "revoke", code], input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("invite revoked", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertTrue(registry["invites"][engine._hash_secret(code)].get("revokedAt"))

    def test_legacy_top_level_revoke_and_restore_still_work(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        result = invoke(["revoke", code] + self.dir_opt, input="y\n")  # pre-0.5.7 spelling
        self.assertEqual(result.exit_code, 0)
        self.assertIn("moved to `awewarm-hub invite revoke`", result.output)
        result = invoke(["restore", code] + self.dir_opt)  # pre-0.5.7 spelling
        self.assertEqual(result.exit_code, 0)
        self.assertIn("moved to `awewarm-hub invite restore`", result.output)
        self.assertIn(joined["tenantId"], result.output)

    def test_restore_by_tenant_id_is_refused_with_guidance(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        result = invoke(["invite", "restore", joined["tenantId"]] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer addressable", result.output)
        self.assertIn("hub list invites --reveal", result.output)

    def test_restore_revives_a_suspended_tenant(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        engine_hub.revoke(code)
        result = invoke(["invite", "restore", code] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("restored", result.output)
        self.assertIn(joined["tenantId"], result.output)
        self.assertFalse(engine.Hub(self.data_dir).registry["invites"][engine._hash_secret(code)].get("revokedAt"))

    def test_restore_of_an_unrevoked_invite_is_refused(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["invite", "restore", code] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not revoked", result.output)

    def test_restore_revives_a_revoked_invite(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.revoke(code)
        result = invoke(["invite", "restore", code] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("pairs again", result.output)
        self.assertTrue(engine_hub.join(code)["token"].startswith("awt_"))

    def test_list_users_marks_suspended_tenants(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.join(code)
        engine_hub.revoke(code)
        result = invoke(["list", "users"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("suspended", result.output)

    def test_invites_shows_a_revoked_code(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.revoke(code)
        result = invoke(["list", "invites"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("revoked", result.output)

    def test_invites_shows_a_suspended_tenant_behind_a_used_code(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.join(code)
        engine_hub.revoke(code)  # the suspension surfaces on the code's row
        result = invoke(["list", "invites"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("revoked", result.output)

    def test_revoke_reports_an_unknown_code(self):
        result = invoke(["invite", "revoke", "awi_" + "x" * 20] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no such invite", result.output)
        self.assertIn("hub list invites", result.output)

    def test_list_with_no_tenants_still_shows_pending_invites(self):
        engine_hub = engine.Hub(self.data_dir)
        engine_hub.mint_invite("alice")
        result = invoke(["list", "users"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No tenants paired yet", result.output)
        self.assertIn("1 invite(s) minted and unused", result.output)
        self.assertIn("hub list invites", result.output)

    def test_invites_masks_codes_by_default(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["list", "invites"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("STATUS", result.output)
        self.assertIn("pending", result.output)
        self.assertNotIn(code, result.output)
        self.assertIn(code[:8], result.output)

    def test_invites_reveal_reveals_codes(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["list", "invites"] + self.dir_opt + ["--reveal"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn(code, result.output)

    def test_invites_hides_tenant_tokens_by_default(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        result = invoke(["list", "invites"] + self.dir_opt + ["--reveal"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn(joined["token"], result.output)
        self.assertIn("pass --token", result.output)

    def test_invites_token_shows_the_tenant_token(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        result = invoke(["list", "invites"] + self.dir_opt + ["--token"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("TOKEN", result.output)
        self.assertIn(joined["token"], result.output)

    def test_invites_token_marks_rows_without_a_tenant(self):
        engine_hub = engine.Hub(self.data_dir)
        engine_hub.mint_invite("alice")  # pending — no tenant, no token
        result = invoke(["list", "invites"] + self.dir_opt + ["--token"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("—", result.output)

    def test_invites_token_shows_a_dash_for_tenants_that_predate_it(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        path = Path(self.data_dir, "tenants.json")
        registry = json.loads(path.read_text())
        registry["tenants"][joined["tenantId"]].pop("token")  # older versions never stored it
        path.write_text(json.dumps(registry))
        result = invoke(["list", "invites"] + self.dir_opt + ["--token"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("Traceback", result.output)
        self.assertIn("predate", result.output)

    def test_invites_json_follows_token_flag(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        masked = invoke(["list", "invites"] + self.dir_opt + ["--json"])
        self.assertNotIn(joined["token"], masked.output)
        revealed = invoke(["list", "invites"] + self.dir_opt + ["--json", "--token"])
        self.assertEqual(json.loads(revealed.output)[0]["token"], joined["token"])

    def test_invites_shows_used_and_who_used_it(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        result = invoke(["list", "invites"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("used", result.output)
        self.assertIn(joined["tenantId"], result.output)

    def test_invites_json_follows_reveal_flag(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        masked = invoke(["list", "invites"] + self.dir_opt + ["--json"])
        self.assertNotIn(code, masked.output)
        revealed = invoke(["list", "invites"] + self.dir_opt + ["--json", "--reveal"])
        rows = json.loads(revealed.output)
        self.assertEqual(rows[0]["code"], code)
        self.assertEqual(rows[0]["status"], "pending")

    def test_invites_reveal_shows_a_dash_for_codes_never_stored(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        path = Path(self.data_dir, "tenants.json")
        registry = json.loads(path.read_text())
        registry["invites"][engine._hash_secret(code)].pop("code")  # older versions never stored it
        path.write_text(json.dumps(registry))
        result = invoke(["list", "invites"] + self.dir_opt + ["--reveal"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("Traceback", result.output)
        self.assertIn("minted before codes were kept", result.output)

    def test_revoke_unknown_tenant_lists_known_ones(self):
        result = invoke(["invite", "revoke", "t_nope"] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer addressable", result.output)

    def test_invite_stamps_a_machine_cap(self):
        result = invoke(["invite"] + self.dir_opt + ["--name", "alice", "--machines", "3"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("3 machine(s)", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        stamped = next(iter(registry["invites"].values()))
        self.assertEqual(stamped["machines"], 3)

    def test_invite_defaults_the_cap_to_the_serve_flag(self):
        engine.Hub(self.data_dir, max_machines=2).record_launch("127.0.0.1", 1)
        invoke(["invite"] + self.dir_opt)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        stamped = next(iter(registry["invites"].values()))
        self.assertEqual(stamped["machines"], 2)

    def test_invite_rejects_a_non_positive_machine_cap(self):
        result = invoke(["invite"] + self.dir_opt + ["--machines", "0"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--machines must be greater than 0", result.output)

    def test_invites_lists_each_codes_machine_cap(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice", machines=3)
        plain = engine_hub.mint_invite("bob")
        result = invoke(["list", "invites"] + self.dir_opt + ["--reveal"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("MACHINES", result.output)
        self.assertIn("3", result.output)
        plain_row = next(line for line in result.output.splitlines() if plain in line)
        self.assertIn(str(engine_hub.max_machines), plain_row)  # the default was stamped too

    def test_config_persists_the_data_dir_in_awewarm_config(self):
        # the setting lives in awewarm's config.json — the same file the
        # pre-split `awewarm hub config` wrote, so upgrades keep it
        from awewarm import config as cfg
        result = invoke(["config", "--data-dir", "/tmp/persisted"])
        self.assertEqual(result.exit_code, 0)
        # storage and display normalize to native separators on Windows
        stored = (cfg.load_config().get("global") or {}).get("serverDataDir")
        self.assertEqual(Path(stored), Path("/tmp/persisted"))
        shown = invoke(["config"])
        self.assertIn(str(Path("/tmp/persisted")), shown.output)
        self.assertIn("set with: awewarm-hub config --data-dir", shown.output)
        invoke(["config", "--unset"])
        shown = invoke(["config"])
        self.assertIn("the default", shown.output)

    def test_config_unset_and_data_dir_conflict(self):
        result = invoke(["config", "--data-dir", "/x", "--unset"])
        self.assertNotEqual(result.exit_code, 0)

    def test_config_sets_and_shows_the_caps(self):
        invoke(["config", "--data-dir", str(self.data_dir)])  # persist it, like a real setup
        result = invoke(["config", "--max-tenants", "20", "--max-conns-per-tenant", "10", "--max-machines", "2"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Caps now 20 tenants, 10 connections each, 2 machine(s) per invite", result.output)
        self.assertIn("without a restart", result.output)
        shown = invoke(["config"])
        for line in ("max tenants: 20 (saved)", "max conns per tenant: 10 (saved)", "max machines per invite: 2 (saved)"):
            self.assertIn(line, shown.output)
        # what serve would launch with: the saved caps win over the code defaults
        self.assertEqual(engine.Hub(self.data_dir).max_tenants, 20)
        self.assertEqual(engine.Hub(self.data_dir).max_machines, 2)

    def test_config_reset_clears_the_caps(self):
        invoke(["config", "--data-dir", str(self.data_dir)])  # persist it, like a real setup
        invoke(["config", "--max-tenants", "20"])
        result = invoke(["config", "--reset"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Caps now 10 tenants, 5 connections each, 1 machine(s) per invite", result.output)
        shown = invoke(["config"])
        self.assertIn("max tenants: 10 (the default)", shown.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertNotIn("maxTenants", registry.get("serve") or {})

    def test_config_rejects_bad_cap_values_and_combinations(self):
        result = invoke(["config", "--max-tenants", "0"] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--max-tenants must be greater than 0", result.output)
        result = invoke(["config", "--max-tenants", "20", "--reset"] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("either --reset or cap values", result.output)
        result = invoke(["config", "--unset", "--max-tenants", "20"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("either --unset or the cap flags", result.output)

    def test_config_data_dir_selects_the_registry_for_cap_changes(self):
        # with cap flags, --data-dir behaves like on every other command:
        # a one-shot registry selector, not the machine's persisted default
        other = Path(self.data_dir).parent / "other-hub"
        result = invoke(["config", "--data-dir", str(other), "--max-tenants", "7"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(engine.Hub(other).max_tenants, 7)
        from awewarm import config as cfg
        self.assertIsNone((cfg.load_config().get("global") or {}).get("serverDataDir"))  # not persisted

    def test_status_shows_pre_set_caps_before_any_launch(self):
        invoke(["config", "--max-tenants", "20"] + self.dir_opt)
        result = invoke(["status"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("tenants: 0/20 active", result.output)
        self.assertIn("never launched", result.output)


class ConnectHubTests(IsolatedTestCase):
    """The user side: open-source `awewarm remote connect` against a live hub."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "hub"
        self.hub, self.httpd = engine.make_hub_server(self.data_dir, "127.0.0.1", 0)
        self.server_thread = start_http_server(self.httpd)
        self.addCleanup(stop_http_server, self.httpd, self.server_thread)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def test_connect_with_invite_stores_a_working_token(self):
        code = self.hub.mint_invite("alice")
        result = invoke_client(["remote", "connect", self.url, "--invite", code])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Joined", result.output)
        from awewarm import config as cfg
        self.assertEqual(cfg.load_config()["remote"]["url"], self.url)
        view = remote_client.ensure_session(cfg.load_config())  # no invite needed anymore
        self.assertTrue(view["tenant"].startswith("t_"))

    def test_connect_prints_the_token_for_safekeeping(self):
        code = self.hub.mint_invite("alice")
        result = invoke_client(["remote", "connect", self.url, "--invite", code])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("keep a copy", result.output)
        shown = next(line.strip() for line in result.output.splitlines() if line.strip().startswith("awt_"))
        self.assertEqual(shown, remote_client.load_token())  # what you see is what got stored

    def test_connect_recovers_with_a_saved_token_after_loss(self):
        joined = remote_client.join(self.url, self.hub.mint_invite("alice"))
        from awewarm import keystore
        keystore._write_secrets({})  # the local copy is gone
        result = invoke_client(["remote", "connect", self.url, "--token", joined["token"]])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("already paired", result.output)
        self.assertEqual(keystore.load_api_key(f"file:{remote_client.TOKEN_SECRET_ID}"), joined["token"])

    def test_connect_prompts_for_the_invite_when_not_given(self):
        code = self.hub.mint_invite("alice")
        result = invoke_client(["remote", "connect", self.url], input=code + "\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Joined", result.output)

    def test_connect_reuses_a_working_stored_token(self):
        joined = remote_client.join(self.url, self.hub.mint_invite("alice"))
        remote_client.store_token(joined["token"])
        self.hub.mint_invite("bob")  # must stay unused: no invite was needed
        result = invoke_client(["remote", "connect", self.url])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("already paired", result.output)
        pending = [e for e in self.hub.registry["invites"].values() if not e.get("usedBy")]
        self.assertEqual(len(pending), 1)

    def test_connect_recovers_from_a_revoked_stored_token(self):
        code = self.hub.mint_invite("alice")
        joined = remote_client.join(self.url, code)
        remote_client.store_token(joined["token"])
        self.hub.revoke(code)
        fresh = self.hub.mint_invite("alice")
        result = invoke_client(["remote", "connect", self.url, "--invite", fresh])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("rejected", result.output)  # the old token's 401 was reported
        self.assertIn("Joined", result.output)
