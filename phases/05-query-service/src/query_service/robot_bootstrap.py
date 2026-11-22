"""Robot onboarding delivery: `GET /robot`, the agent tarball and the commands.

The vault serves its own robot bootstrap script and agent build, so a robot
needs no outside host. The trust chain has no signing keys:

1. The admin copies the install command from an authenticated HTTPS page. It
   carries the SHA-256 of robot.sh, computed here from the bytes `GET /robot`
   serves.
2. robot.sh carries the size and SHA-256 of the agent tarball (rendered at
   image build time).
3. The enrollment code carries the SPKI pin of the ingest CA (`GET /ca.crt`).

The script and the tarball are public material; nothing secret is served.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Response
from fastapi.responses import FileResponse, JSONResponse

from . import settings

logger = logging.getLogger(__name__)

SCRIPT_NAME = "robot.sh"
AGENT_TARBALL_NAME = "agent-linux-amd64.tar.gz"
# robot.sh is tens of kilobytes. The bound keeps a wrong path from turning an
# unauthenticated route into an unbounded file read.
SCRIPT_MAX_BYTES = 1024 * 1024
# Same host:port rule as the UI's normalizeIngestEndpoint (ui-spa/src/lib/enrollment.ts).
_INGEST_ENDPOINT = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\]):([0-9]{1,5})")
_SHELL_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
_AGENT_VERSION_LINE = re.compile(rb"^AGENT_VERSION=['\"]?([0-9A-Za-z][0-9A-Za-z.+~-]{0,63})['\"]?$", re.M)


def shell_quote(value: str) -> str:
    """Byte-identical to the UI's shellQuote, so server and fallback commands match."""
    if _SHELL_SAFE.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def valid_ingest_endpoint(value: object) -> bool:
    match = _INGEST_ENDPOINT.fullmatch(value) if isinstance(value, str) else None
    return match is not None and 1 <= int(match.group(1)) <= 65535


def _bootstrap_file(name: str) -> Optional[Path]:
    root = (settings.ROBOT_BOOTSTRAP_DIR or "").strip()
    return Path(root) / name if root else None


def read_script() -> Optional[bytes]:
    """The exact robot.sh bytes, or None when the file is missing or unusable."""
    path = _bootstrap_file(SCRIPT_NAME)
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(SCRIPT_MAX_BYTES + 1)
    except OSError as exc:
        logger.warning("Robot bootstrap script unreadable (%s)", type(exc).__name__)
        return None
    if not data or len(data) > SCRIPT_MAX_BYTES:
        logger.warning("Robot bootstrap script is empty or exceeds %d bytes", SCRIPT_MAX_BYTES)
        return None
    return data


def agent_version(script: bytes) -> Optional[str]:
    match = _AGENT_VERSION_LINE.search(script)
    return match.group(1).decode("ascii") if match else None


def vault_url() -> Optional[str]:
    """The public HTTPS origin robots use, or None when no command can work."""
    base = settings.PUBLIC_BASE_URL
    if not base:
        return None
    parts = urlsplit(base)
    # robot.sh and the command use `--proto =https`; an http or path-bearing
    # URL would give the admin a command that cannot succeed.
    if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.fragment:
        return None
    return base


def _download_and_check(vault: str, script_sha256: str) -> str:
    # mktemp: no predictable /tmp name (symlink and swap races). The digest
    # check runs before the script, so substituted bytes never execute.
    return (
        f"f=$(mktemp) && curl -fsSk --proto =https {shell_quote(vault + '/robot')} -o \"$f\" \\\n"
        f"  && echo \"{script_sha256}  $f\" | sha256sum -c - \\\n"
        "  && sudo sh \"$f\""
    )


def install_command(
    *,
    vault: str,
    script_sha256: str,
    code: str,
    reenroll: bool,
    ingest_endpoint: str,
    recording_root: Optional[str],
) -> str:
    if not valid_ingest_endpoint(ingest_endpoint):
        raise ValueError("ingest endpoint is not host:port")
    command = _download_and_check(vault, script_sha256)
    if reenroll:
        command += " --reenroll"
    command += f" --vault {shell_quote(vault)} --ingest {shell_quote(ingest_endpoint)}"
    command += f" --code {shell_quote(code)}"
    if recording_root is not None:
        command += f" --root {shell_quote(recording_root)}"
    return command


def upgrade_command(*, vault: str, script_sha256: str) -> str:
    return _download_and_check(vault, script_sha256) + f" --upgrade --vault {shell_quote(vault)}"


def _unavailable() -> JSONResponse:
    # Never the path or the OS message: these routes are unauthenticated.
    return JSONResponse({"error": "robot_bootstrap_unavailable"}, status_code=503)


def robot_script() -> Response:
    """The robot bootstrap script, byte for byte what the install command hashes."""
    data = read_script()
    if data is None:
        return _unavailable()
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def robot_agent_tarball() -> Response:
    """The agent tarball; robot.sh checks its baked size and SHA-256 before use."""
    path = _bootstrap_file(AGENT_TARBALL_NAME)
    if path is None or not path.is_file():
        return _unavailable()
    return FileResponse(
        path,
        media_type="application/gzip",
        headers={"Cache-Control": "no-store"},
    )


def script_sha256(script: Optional[bytes]) -> Optional[str]:
    return hashlib.sha256(script).hexdigest() if script is not None else None
