"""awewarm-hub serve: many users behind one hub process.

Covers the invite→token pairing flow, tenant isolation (two users may both
delegate a connection named glm), quotas, the restart story (pairings persist
via hashed tokens while API keys stay RAM-only), revocation, and the client
`remote connect` flow (open-source awewarm) against a live hub.
"""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
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

    def test_registry_stores_hashes_never_plaintext(self):
        token, _ = self.join("alice")
        on_disk = (self.data_dir / "tenants.json").read_text()
        self.assertNotIn(token, on_disk)
        for record in self.registry()["tenants"].values():
            self.assertEqual(len(record["tokenHash"]), 64)  # sha256 hex
        self.assertFalse(list(self.data_dir.rglob("secrets.json")))

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

    def test_restore_brings_the_token_back(self):
        token, tenant_id = self.join("alice")
        code = self.code_of(tenant_id)
        self.hub.revoke(code)
        self.hub.restore(code)
        self.assertEqual(remote_client.fetch_state(self.url, token)["connections"], {})
        self.assertFalse(self.registry()["invites"][engine._hash_secret(code)].get("revokedAt"))

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


class CrossProcessTests(HubCase):
    """A serve that outlives operator commands: invites minted and tenants
    revoked by separate one-shot processes (the hub CLI) must be honored
    without a restart."""

    def test_join_honors_an_invite_minted_by_another_process(self):
        operator = engine.Hub(self.data_dir)  # what `awewarm-hub invite` runs as
        code = operator.mint_invite("alice")
        joined = remote_client.join(self.url, code)  # against the long-lived serve
        self.assertTrue(joined["token"].startswith("awt_"))

    def test_revoked_token_stops_working_without_a_restart(self):
        token, tenant_id = self.join("alice")
        engine.Hub(self.data_dir).revoke(self.code_of(tenant_id))  # `awewarm-hub revoke` in another process
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token)
        self.assertIn("401", str(ctx.exception))

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
        result = invoke(["invite"] + self.dir_opt + ["--note", "alice"])
        self.assertEqual(result.exit_code, 0)
        code = next(line.strip() for line in result.output.splitlines() if line.strip().startswith("awi_"))
        self.assertTrue(engine.INVITE_RE.match(code))
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertIn(engine._hash_secret(code), registry["invites"])
        self.assertEqual(registry["invites"][engine._hash_secret(code)]["note"], "alice")
        self.assertEqual(registry["invites"][engine._hash_secret(code)]["code"], code)

    def test_invite_reports_a_busy_registry_without_a_traceback(self):
        busy = ApiError(503, "hub registry is busy — retry this request")
        with mock.patch.object(engine.Hub, "mint_invite", side_effect=busy):
            result = invoke(["invite"] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not mint the invite", result.output)
        self.assertIn("hub registry is busy", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_invite_tells_the_operator_the_user_command(self):
        result = invoke(["invite"] + self.dir_opt + ["--note", "alice"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("awewarm remote connect <hub-url> --invite awi_", result.output)

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
        result = invoke(["revoke", joined["tenantId"]] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer addressable", result.output)
        self.assertIn("hub list invites --reveal", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertNotIn("suspendedAt", registry["tenants"][joined["tenantId"]])  # untouched

    def test_revoke_by_code_suspends_the_tenant(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        result = invoke(["revoke", code] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("suspended", result.output)
        self.assertIn(joined["tenantId"], result.output)
        self.assertIn("awewarm-hub restore", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        record = registry["tenants"][joined["tenantId"]]
        self.assertNotIn("suspendedAt", record)  # nothing deleted, nothing mirrored
        self.assertTrue(registry["invites"][engine._hash_secret(code)]["revokedAt"])

    def test_revoke_reports_a_busy_registry_without_a_traceback(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        busy = ApiError(503, "hub registry is busy — retry this request")
        with mock.patch.object(engine.Hub, "revoke", side_effect=busy):
            result = invoke(["revoke", code] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not revoke the invite", result.output)
        self.assertIn("hub registry is busy", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_revoke_kills_a_pending_invite(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["revoke", code] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("invite revoked for alice", result.output)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertTrue(registry["invites"][engine._hash_secret(code)].get("revokedAt"))
        with self.assertRaises(ApiError):
            engine_hub.join(code)

    def test_revoke_aborts_without_confirmation(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["revoke", code] + self.dir_opt, input="n\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("aborted", result.output)
        self.assertTrue(engine_hub.join(code)["token"].startswith("awt_"))

    def test_restore_by_tenant_id_is_refused_with_guidance(self):
        engine_hub = engine.Hub(self.data_dir)
        joined = engine_hub.join(engine_hub.mint_invite("alice"))
        result = invoke(["restore", joined["tenantId"]] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer addressable", result.output)
        self.assertIn("hub list invites --reveal", result.output)

    def test_restore_revives_a_suspended_tenant(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        joined = engine_hub.join(code)
        engine_hub.revoke(code)
        result = invoke(["restore", code] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("restored", result.output)
        self.assertIn(joined["tenantId"], result.output)
        self.assertFalse(engine.Hub(self.data_dir).registry["invites"][engine._hash_secret(code)].get("revokedAt"))

    def test_restore_of_an_unrevoked_invite_is_refused(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        result = invoke(["restore", code] + self.dir_opt)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not revoked", result.output)

    def test_restore_revives_a_revoked_invite(self):
        engine_hub = engine.Hub(self.data_dir)
        code = engine_hub.mint_invite("alice")
        engine_hub.revoke(code)
        result = invoke(["restore", code] + self.dir_opt)
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
        result = invoke(["revoke", "awi_" + "x" * 20] + self.dir_opt, input="y\n")
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

    def test_invites_rejects_the_removed_token_flag(self):
        result = invoke(["list", "invites"] + self.dir_opt + ["--token"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such option", result.output)

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
        result = invoke(["revoke", "t_nope"] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer addressable", result.output)

    def test_invite_stamps_a_machine_cap(self):
        result = invoke(["invite"] + self.dir_opt + ["--note", "alice", "--machines", "3"])
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
        self.assertEqual(
            (cfg.load_config().get("global") or {}).get("serverDataDir"), "/tmp/persisted"
        )
        shown = invoke(["config"])
        self.assertIn("/tmp/persisted", shown.output)
        self.assertIn("set with: awewarm-hub config --data-dir", shown.output)
        invoke(["config", "--unset"])
        shown = invoke(["config"])
        self.assertIn("the default", shown.output)

    def test_config_unset_and_data_dir_conflict(self):
        result = invoke(["config", "--data-dir", "/x", "--unset"])
        self.assertNotEqual(result.exit_code, 0)


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
