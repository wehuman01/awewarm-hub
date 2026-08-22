"""Shared test utilities: env-isolated paths and connection factories.

Mirrors awewarm's tests/helpers.py for the pieces the hub tests use; the hub
CLI and engine read the same awewarm config/state env vars."""
import os
import tempfile
import threading
import unittest
from pathlib import Path

ENV_KEYS = ("AWEWARM_CONFIG", "AWEWARM_STATE", "AWEWARM_LOG", "AWEWARM_PLIST", "AWEWARM_SYSTEMD_DIR")


def start_http_server(httpd):
    """Run a test HTTP server with a short shutdown polling interval."""
    thread = threading.Thread(
        target=httpd.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    return thread


def stop_http_server(httpd, thread):
    """Stop the loop, close its listening socket, and reap its thread."""
    if thread.is_alive():
        httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=1)


class IsolatedTestCase(unittest.TestCase):
    """Point every awewarm path at a temp dir so tests never touch the user's."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.tmp_path = base
        overrides = {
            "AWEWARM_CONFIG": str(base / "config.json"),
            "AWEWARM_STATE": str(base / "state.json"),
            "AWEWARM_LOG": str(base / "awewarm.log"),
            "AWEWARM_PLIST": str(base / "agent.plist"),
            "AWEWARM_SYSTEMD_DIR": str(base / "systemd-user"),
        }
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ.update(overrides)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def plan_connection(mode="fixed", fixed_at=("06:35",), days="weekday", window_status="unknown", duration=None):
    confirmed = window_status == "user-confirmed"
    return {
        "label": "GLM Coding Plan",
        "kind": "subscription",
        "enabled": True,
        "auth": {"type": "api-key", "status": "valid", "apiKeyRef": "${AWEWARM_API_KEY_GLM_CODING_PLAN}"},
        "transport": {
            "kind": "anthropic-messages",
            "baseUrl": "https://open.bigmodel.cn/api/anthropic",
            "cliCommand": None,
        },
        "plan": {"url": "https://example.com/plan", "label": "GLM Coding Plan"},
        "window": {
            "status": window_status,
            "startRule": "unknown",
            "durationMinutes": duration,
            "evidence": "user-confirmed" if confirmed else "none",
        },
        "activation": {"model": "glm-4.7", "prompt": "Reply with exactly: ok", "maxTokens": 4},
        "catchup": {"attempts": 5, "withinMinutes": 30},
        "degradeAfterNodes": 3,
        "schedule": {
            "mode": mode,
            "fixed": {
                "at": list(fixed_at),
                "days": days,
                "skipIfActivatedWithinMinutes": 30,
            },
            "interval": {"graceSeconds": 75, "jitterSeconds": 30},
        },
    }
