"""`ingotron-agent status`.

Prints: robot id, vault + ingest endpoints, credential fingerprint and age, CA
pin match, last run result and time, queue depth by manifest status, bytes
pending, the timer's next run, and the agent version.

**It never prints the token**, and it never writes anything -- the queue walk is
read-only, so running `status` in a loop cannot create manifests the uploader
would then have to reconcile.  A test asserts both halves: no 52-character
base32 run anywhere in the output, and zero new manifest files afterwards.

**It also never opens a channel, and specifically never asks
`UploadService.HealthCheck` whether the credential is good**.  HealthCheck
does not authenticate: a fabricated `ignr1.` token receives
`{"status":"healthy"}`, because `health_check()` never calls
`resolve_request_identity` and the interceptor rejects only a *malformed*
bearer.  Anything that must demonstrate a credential works uses `ListUploads`,
`GetUploadStatus` or `Upload`.  Reporting a revoked robot as
reachable-and-authenticated during an incident is the one failure this command
must not have.

The queue walk here is deliberately NOT `UploadQueue.describe()`:
`describe()` returns `pending[:10]`, a sample
capped at ten, and carries no per-status breakdown -- summing its sizes reports
the first ten files of a four-hundred-file backlog as if it were the whole
queue, on the one command an operator runs to find out how far behind a robot
is.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import AGENT_VERSION, crockford, paths


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ca_pin_state() -> Dict[str, Any]:
    """Recompute the CA pin from the installed certificate and compare.

    The installer stores the full SPKI hash at /etc/ingotron/ca.spki.sha256 for
    exactly this: a vault whose CA key was swapped under a robot shows up
    here as a mismatch rather than as a confusing TLS error at upload time.
    """
    out: Dict[str, Any] = {"ca_cert": str(paths.CA_CERT), "match": None}
    if not paths.CA_CERT.is_file() or not paths.CA_SPKI_SHA256.is_file():
        out["error"] = "not enrolled: no CA certificate or recorded pin"
        return out
    recorded = paths.CA_SPKI_SHA256.read_text(encoding="ascii").strip()
    try:
        spki_der = subprocess.run(
            ["openssl", "pkey", "-pubin", "-outform", "DER"],
            input=subprocess.run(
                ["openssl", "x509", "-in", str(paths.CA_CERT), "-noout", "-pubkey"],
                check=True,
                capture_output=True,
            ).stdout,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        out["error"] = f"could not read the installed CA: {exc}"
        return out
    import hashlib

    observed = hashlib.sha256(spki_der).hexdigest()
    out["recorded_spki_sha256"] = recorded
    out["observed_spki_sha256"] = observed
    out["match"] = observed == recorded
    out["pin"] = crockford.ca_pin_from_spki_digest_hex(observed)
    if not out["match"]:
        out["error"] = (
            "the installed CA does not match the one this robot enrolled against. "
            "Uploads will fail until the vault CA is restored or the robot is "
            "re-enrolled."
        )
    return out


def timer_state(unit: str = "ingotron-agent.timer") -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["systemctl", "list-timers", "--no-pager", "--no-legend", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"unit": unit, "error": str(exc)}
    if proc.returncode != 0:
        return {"unit": unit, "error": proc.stderr.strip() or f"exit {proc.returncode}"}
    line = proc.stdout.strip()
    return {"unit": unit, "next": line or "not scheduled"}


def queue_state(config_path: Path) -> Dict[str, Any]:
    """Queue depth BY MANIFEST STATUS and bytes pending -- READ ONLY.

    This walks the tree itself rather than reading `UploadQueue.describe()`,
    and the reason is not stylistic. `describe()` returns `"pending": pending[:10]`
    (see UploadQueue.describe in core.py) -- a SAMPLE, capped at ten -- alongside an accurate
    `pending_count`. Summing sizes over that list would report the bytes of the
    first ten files of a four-hundred-file backlog as if it were the whole
    queue, which is the number an operator uses to decide whether a robot is
    stuck. `describe()` also has no per-status breakdown at all, and `status`
    must print one.

    The walk uses `load_or_create_manifest`, which returns an in-memory dict
    and writes nothing when a manifest is absent -- only `save_manifest`
    persists. That is what keeps `status` free of side effects.
    """
    # Deliberately imported from the storage/config packages rather than from
    # `robot_upload_agent.core`, which drags in prometheus_client and the gRPC
    # transport. `status` must work on a robot whose problem IS that the
    # transport cannot load.
    try:
        from robot_storage_manager.core import load_or_create_manifest, scan_for_data_files
        from robot_vault_config.model import load_config
        from robot_vault_config.upload_status import is_retryable
    except ImportError as exc:  # pragma: no cover
        return {"error": f"agent packages unavailable: {exc}"}
    if not config_path.is_file():
        return {"error": f"{config_path} does not exist"}
    cfg = load_config(config_path)
    root = Path(cfg.paths.robot_root)
    if not root.is_dir():
        return {"root": str(root), "error": "the recordings tree does not exist"}

    by_status: Dict[str, int] = {}
    bytes_pending = 0
    pending_files = 0
    total_files = 0
    for path in scan_for_data_files(root):
        total_files += 1
        manifest = load_or_create_manifest(path, cfg=cfg)
        status = str(manifest.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        if is_retryable(status):
            pending_files += 1
            try:
                bytes_pending += path.stat().st_size
            except OSError:
                pass

    return {
        "root": str(root),
        "total_files": total_files,
        "pending_files": pending_files,
        "bytes_pending": bytes_pending,
        "by_status": dict(sorted(by_status.items())),
    }


def build(config_path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = config_path or paths.VAULT_CONFIG
    enrollment = _read_json(paths.ENROLLMENT_STATE) or {}
    last_run = _read_json(paths.LAST_RUN) or {}

    credential: Dict[str, Any] = {
        "token_file": str(paths.TOKEN_FILE),
        "present": paths.TOKEN_FILE.is_file(),
        "fingerprint": enrollment.get("credential_fingerprint"),
    }
    if credential["present"]:
        stat = paths.TOKEN_FILE.stat()
        credential["mode"] = oct(stat.st_mode & 0o777)
        credential["age_seconds"] = int(time.time() - stat.st_mtime)
    credential["previous_present"] = paths.TOKEN_FILE_PREV.is_file()

    self_rotation = enrollment.get("self_rotation_enabled")
    report: Dict[str, Any] = {
        "agent_version": AGENT_VERSION,
        "robot_id": enrollment.get("robot_id"),
        "vault_url": enrollment.get("vault_url"),
        "ingest_endpoint": enrollment.get("ingest_endpoint"),
        "enrolled_at": enrollment.get("enrolled_at"),
        "config": str(config_path),
        "credential": credential,
        "ca": ca_pin_state(),
        "self_rotation": (
            "enabled"
            if self_rotation
            else ("disabled by the vault" if self_rotation is False else "unknown")
        ),
        "timer": timer_state(),
        "queue": queue_state(config_path),
        "last_run": {
            "exit_status": last_run.get("exit_status"),
            "finished_at": last_run.get("finished_at"),
            "stages": [
                {k: s.get(k) for k in ("stage", "exit_status", "ok", "result")}
                for s in (last_run.get("stages") or [])
            ],
        },
    }
    if self_rotation:
        report["rotate_timer"] = timer_state("ingotron-agent-rotate.timer")
    return report


def main(config_path: Optional[Path] = None) -> int:
    print(json.dumps(build(config_path), indent=2, sort_keys=True))
    return 0
