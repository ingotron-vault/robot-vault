"""`ingotron-agent-run` -- the scan -> hash -> upload one-shot.

Why a wrapper and not a shell line in ExecStart: `rsm` and `rua` ignore
VAULT_CONFIG_PATH and must be given `--config` explicitly, and the run needs a
single-instance lock that neither CLI has.  `UploadQueue` takes no lock of its
own -- core.py's `_upload_one` docstring says so in as many words -- so an
operator running a manual upload during a timer run would otherwise race.

Order, exactly:
    flock(/var/lib/ingotron/agent.lock, LOCK_EX|LOCK_NB)   -- exit 0 with
                                                              {"skipped":"locked"}
                                                              if held
    rsm scan   --config /etc/ingotron/vault.json --root <root>
    rsm hash   --config /etc/ingotron/vault.json --root <root>
    rua upload --all --config /etc/ingotron/vault.json --root <root>
    write /var/lib/ingotron/state/last_run.json
and one JSON line per stage to the journal.
"""

import contextlib
import fcntl
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import AGENT_VERSION, paths

EXIT_OK = 0
EXIT_STAGE_FAILED = 1
EXIT_CONFIG = 2


class LockHeld(Exception):
    pass


@contextlib.contextmanager
def single_instance(lock_path: Path):
    """flock(LOCK_EX|LOCK_NB).  Raises LockHeld rather than blocking."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockHeld(str(exc)) from exc
        yield
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def emit(record: Dict[str, Any]) -> None:
    """One JSON object per line, to stdout, which systemd routes to the journal."""
    print(json.dumps(record, sort_keys=True), flush=True)


def load_vault(config_path: Path):
    from robot_vault_config.model import load_config

    cfg = load_config(config_path)
    cfg.validate_tls()
    return cfg


def assert_enrolled_posture(cfg, config_path: Path) -> None:
    """Refuse to run an enrolled robot on an unverified transport.

    The vendored transport still has an `insecure_channel` branch for the
    in-compose development topology.  On a robot installed by robot.sh that
    branch must be unreachable, and "unreachable by configuration" is only a
    property if something asserts it -- so this does, at the top of every run,
    with a non-zero exit rather than a warning.
    """
    if not cfg.tls.enabled:
        raise SystemExit(
            _fail(
                EXIT_CONFIG,
                f"{config_path} has TLS disabled. An enrolled robot must not upload over "
                "an unverified channel; re-run `ingotron-agent enroll` or restore the "
                "vault.json robot.sh wrote.",
            )
        )
    if not cfg.tls.ca_path:
        raise SystemExit(
            _fail(EXIT_CONFIG, f"{config_path} has tls.enabled but no tls.ca_path")
        )
    token_file = getattr(getattr(cfg, "auth", None), "token_file", None)
    if not token_file:
        raise SystemExit(
            _fail(
                EXIT_CONFIG,
                f"{config_path} names no credential. The installer stores the per-robot "
                "token at a path referenced by auth.token_file.",
            )
        )


def _fail(code: int, message: str) -> int:
    emit({"stage": "preflight", "ok": False, "error": message})
    return code


def _run_cli(name: str, main, argv: List[str]) -> Dict[str, Any]:
    """Run a vendored CLI in-process and capture its exit status and JSON output.

    In-process rather than subprocess because the bundle vendors the packages
    and has no console-script shims; the argv list is byte-identical to the documented
    command line, and the exit status is the CLI's own.
    """
    started = time.time()
    buf = io.StringIO()
    errbuf = io.StringIO()
    status: Optional[int]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(errbuf):
            status = int(main(argv))
    except SystemExit as exc:  # argparse exits this way on a bad argv
        # exc.code is an int for argparse but is allowed to be a string or any
        # object; int() on a string would raise out of the handler and kill the
        # run, which is the one thing this wrapper exists to prevent.
        try:
            status = int(exc.code or 0)
        except (TypeError, ValueError):
            status = 2
    except Exception as exc:  # noqa: BLE001 - a stage failure must not kill the run
        status = 70
        errbuf.write(f"{type(exc).__name__}: {exc}")
    payload: Any = None
    text = buf.getvalue().strip()
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"stdout": text[:2000]}
    record = {
        "stage": name,
        "argv": argv,
        "exit_status": status,
        "ok": status == 0,
        "duration_seconds": round(time.time() - started, 3),
    }
    stderr_text = errbuf.getvalue().strip()
    if stderr_text:
        record["stderr"] = stderr_text[:2000]
    if payload is not None:
        record["result"] = _summarise(name, payload)
    return record


def _summarise(name: str, payload: Any) -> Any:
    """Keep the journal line small and free of anything secret-shaped."""
    if not isinstance(payload, dict):
        return payload
    if name == "upload":
        return {
            "uploaded": len(payload.get("uploaded") or []),
            "errors": payload.get("errors") or [],
            "skipped": len(payload.get("skipped") or []),
            "connection_quality": payload.get("connection_quality"),
        }
    keys = ("scanned", "created", "updated", "hashed", "failed", "errors")
    return {k: payload[k] for k in keys if k in payload}


def run_once(config_path: Path) -> int:
    cfg = load_vault(config_path)
    assert_enrolled_posture(cfg, config_path)
    root = cfg.paths.robot_root

    from robot_storage_manager.cli import main as rsm_main
    from robot_upload_agent.cli import main as rua_main

    common = ["--config", str(config_path), "--root", str(root)]
    stages = [
        ("scan", rsm_main, ["scan", *common]),
        ("hash", rsm_main, ["hash", *common]),
        ("upload", rua_main, ["upload", "--all", *common]),
    ]

    started_at = time.time()
    records: List[Dict[str, Any]] = []
    exit_status = EXIT_OK
    for name, main, argv in stages:
        record = _run_cli(name, main, argv)
        emit(record)
        records.append(record)
        if not record["ok"]:
            # A failed scan makes the following stages meaningless, so stop.
            exit_status = EXIT_STAGE_FAILED
            break

    summary = {
        "started_at": started_at,
        "finished_at": time.time(),
        "agent_version": AGENT_VERSION,
        "exit_status": exit_status,
        "stages": records,
    }
    write_last_run(summary)
    emit({"stage": "run", "ok": exit_status == EXIT_OK, "exit_status": exit_status})
    return exit_status


def write_last_run(summary: Dict[str, Any]) -> None:
    """Durable run summary, so `status` works with a volatile journald."""
    try:
        paths.LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(paths.LAST_RUN) + ".new")
        tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(paths.LAST_RUN))
    except OSError as exc:  # pragma: no cover
        print(f"warning: could not write {paths.LAST_RUN}: {exc}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config_path = paths.VAULT_CONFIG
    if "--config" in args:
        config_path = Path(args[args.index("--config") + 1])
    if not config_path.is_file():
        return _fail(EXIT_CONFIG, f"{config_path} does not exist; this robot is not enrolled")
    try:
        with single_instance(paths.LOCK_FILE):
            return run_once(config_path)
    except LockHeld:
        # Exit 0: another run holds the lock, which is the timer working as
        # designed, not a failure to alert on.
        emit({"stage": "run", "skipped": "locked", "ok": True})
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
