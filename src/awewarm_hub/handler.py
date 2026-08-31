"""The hub-mode HTTP seams over awewarm's single-tenant `_Handler` core.

Every difference from single-tenant serving lives in one of these seam
overrides: /v1/join exists, /v1/claim does not, auth resolves a tenant's
private workspace, quotas gate PUT, release never frees a slot, and usage
rides on manual runs. Everything else (routing, plumbing, the wire
protocol) is inherited from awewarm.server.
"""
from awewarm.server import ApiError, _Handler

from . import __version__


class HubHandler(_Handler):
    hub = None  # bound by engine.make_hub_server

    def _engine(self):
        return self.hub

    def _healthz_payload(self):
        return {"ok": True, "version": __version__, "claimed": True, "hub": True}

    def _view_extras(self, warm, tenant):
        # The view's own "version" is the engine package's; without this a
        # hub answers 0.5.x while healthz says 0.5.y and users see a mismatch.
        return {"hubVersion": __version__}

    def _join(self, body, machine):
        return self.hub.join(body.get("invite"), machine)

    def _claim(self, body):
        raise ApiError(403, "this is a hub server — pair with an invite: awewarm remote connect <url>")

    def _resolve(self):
        tenant = self.hub.auth(self._bearer(), self._machine())
        return tenant.warm, tenant

    def _release(self, warm, tenant):
        # The pairing outlives a disconnect — the kept token re-pairs on
        # reconnect; freeing the slot is the operator's call (`awewarm-hub
        # revoke`), not any single client's.
        return {"ok": True, "released": False}

    def _put_connection(self, warm, tenant, conn_id, body):
        return self.hub.put_connection(tenant, conn_id, body)

    def _run_now(self, warm, tenant, conn_id, body):
        return self.hub.run_now(tenant, conn_id, bool(body.get("resetDue")), bool(body.get("allowAutoDisabled")))
