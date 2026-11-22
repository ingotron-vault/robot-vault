"""The on-robot layout.

Every path the agent touches is named here once, so that a test can point the
whole runtime at a sandbox by setting INGOTRON_ROOT_PREFIX instead of patching
module internals.  The prefix is empty in production; it exists so that the
0600/0700 assertions in the test suite run against a real filesystem rather
than a mock.
"""

import os
from pathlib import Path

#: Test-only relocation of the whole tree.  Never set in production; the
#: systemd units do not pass it and robot.sh does not set it.
ROOT_PREFIX = os.environ.get("INGOTRON_ROOT_PREFIX", "")


def _p(absolute: str) -> Path:
    if ROOT_PREFIX:
        return Path(ROOT_PREFIX) / absolute.lstrip("/")
    return Path(absolute)


CONF_DIR = _p("/etc/ingotron")
VAULT_CONFIG = _p("/etc/ingotron/vault.json")
CA_CERT = _p("/etc/ingotron/ca.crt")
CA_SPKI_SHA256 = _p("/etc/ingotron/ca.spki.sha256")
CRED_DIR = _p("/etc/ingotron/credentials")
TOKEN_FILE = _p("/etc/ingotron/credentials/robot.token")
TOKEN_FILE_PREV = _p("/etc/ingotron/credentials/robot.token.prev")
# The service account owns /var/lib/ingotron/state so it can write run state.
# A post-exchange recovery record contains a credential and must never live
# there. This directory is created 0700 by the root-run enrollment command.
RECOVERY_DIR = _p("/etc/ingotron/recovery")
ENROLLMENT_PENDING = RECOVERY_DIR / "enrollment-pending.json"

STATE_DIR = _p("/var/lib/ingotron")
LOCK_FILE = _p("/var/lib/ingotron/agent.lock")
LAST_RUN = _p("/var/lib/ingotron/state/last_run.json")
ENROLLMENT_STATE = _p("/var/lib/ingotron/state/enrollment.json")

AGENT_PREFIX = _p("/opt/ingotron-agent")
CURRENT = _p("/opt/ingotron-agent/current")

UNIT_DIR = _p("/etc/systemd/system")

SERVICE_USER = "ingotron"
SERVICE_GROUP = "ingotron"

#: The enrollment RPC lives on ingest, by default port 50051 on the vault host --
#: not on the query service. `--ingest host:port` names another endpoint.
ENROLLMENT_PORT = 50051
