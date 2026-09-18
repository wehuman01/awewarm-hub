"""Import-face contract between awewarm-hub and the awewarm engine.

The hub reaches into a fixed list of engine names — two of them private
(`awewarm.server._Handler`, `awewarm.config._write_json`) — and pins
`awewarm>=0.7,<0.8` as its engine contract. The module-level imports below ARE
the contract: if the engine renames or moves any of these names, test
collection fails here instead of breaking a hub install at runtime. Re-audit
this file whenever the pin range in pyproject.toml moves.
"""

import awewarm.config
import awewarm.locking
import awewarm.schedule
import awewarm.server
import awewarm.update_check
from awewarm import __version__ as awewarm_version
from awewarm import remote, schedule
from awewarm.config import _write_json
from awewarm.config import append_log, conn_state, die, load_config, save_config, timezone_for
from awewarm.locking import LockBusy, process_lock
from awewarm.server import ApiError, BoundedThreadingHTTPServer, WarmServer
from awewarm.server import _Handler
from awewarm.update_check import get_pypi_latest, version_gte

ENGINE_PIN = "0.7"


def test_engine_version_within_pin():
    assert awewarm_version.startswith(ENGINE_PIN + "."), (
        f"awewarm {awewarm_version} is outside the hub's >=0.7,<0.8 "
        "pin — re-audit this contract file against the new engine import face"
    )
