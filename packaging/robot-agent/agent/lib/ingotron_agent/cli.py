"""`ingotron-agent <cmd>` -- the operator surface.

    enroll   --vault URL [--ingest HOST:PORT] --code CODE [--force]
                                                 exchange over the pinned CA, write
                                                 config + token
    run      [--once]                            what the unit runs
    status                                       never prints the token
    logs     [-n N] [-f]                         journalctl passthrough
    credential rotate [--if-older-than D]
    uninstall [--purge]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import AGENT_VERSION, enroll as enroll_mod, paths, runner, status as status_mod

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED = 3


def _err(message: str) -> int:
    print(f"ingotron-agent: error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _require_root(action: str) -> Optional[int]:
    if os.geteuid() != 0:
        return _err(f"{action} must run as root")
    return None


# --------------------------------------------------------------------------
# enroll
# --------------------------------------------------------------------------


def cmd_enroll(args: argparse.Namespace) -> int:
    if args.resume:
        # No code is read at all on this path: the one that got us here is
        # already spent, and asking for another is the bug this exists to fix.
        try:
            state = enroll_mod.resume(
                config_path=Path(args.config),
                token_file=Path(args.token_file),
                ca_path=Path(args.ca),
                recordings_root=args.root,
                result_file=Path(args.result_file) if args.result_file else None,
            )
        except enroll_mod.EnrollmentError as exc:
            return _err(str(exc))
        except OSError as exc:
            return _err(f"could not complete the resumed enrollment: {exc}")
        print(json.dumps(state, indent=2, sort_keys=True))
        return EXIT_OK

    if args.code_stdin:
        # robot.sh hands the code over on stdin rather than on this process's
        # argv, so the secret does not get a second /proc entry.
        code = sys.stdin.read().strip()
    else:
        code = (args.code or "").strip()
    if not code:
        return _err("no enrollment code supplied (--code, or --code-stdin with the code on stdin)")

    try:
        state = enroll_mod.enroll(
            vault_url=args.vault,
            code=code,
            ca_path=Path(args.ca),
            config_path=Path(args.config),
            token_file=Path(args.token_file),
            recordings_root=args.root,
            agent_version=args.agent_version or AGENT_VERSION,
            result_file=Path(args.result_file) if args.result_file else None,
            force=args.force,
            ingest=args.ingest,
        )
    except enroll_mod.EnrollmentError as exc:
        return _err(str(exc))
    except OSError as exc:
        return _err(f"could not write the enrollment result: {exc}")

    print(json.dumps(state, indent=2, sort_keys=True))
    return EXIT_OK


def _recordings_root_from_config(config_path: Path) -> str:
    """Read the installed recording tree without guessing a replacement.

    A re-enrollment changes the vault credential, not the local recording
    location.  Defaulting to `/data/robot_data` here would silently point an
    already-working robot at a different tree, so this command deliberately
    refuses when the installed config cannot provide a usable path.
    """
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        root = config["paths"]["robot_root"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise enroll_mod.EnrollmentError(
            f"could not read the installed recordings path from {config_path}: {exc}. "
            "Restore vault.json or pass --root explicitly."
        ) from exc
    try:
        return enroll_mod.validate_recordings_root(root)
    except enroll_mod.EnrollmentError as exc:
        raise enroll_mod.EnrollmentError(
            f"{config_path} has no usable paths.robot_root ({exc}); pass --root explicitly."
        ) from exc


def cmd_reenroll(args: argparse.Namespace) -> int:
    """Replace an installed robot's credential while retaining its local data path."""
    guard = _require_root("re-enrollment")
    if guard is not None:
        return guard
    if not paths.TOKEN_FILE.is_file() and not paths.ENROLLMENT_PENDING.is_file():
        return _err(
            f"this robot has no installed credential at {paths.TOKEN_FILE}; "
            "use the first-install command from the Robot Vault UI instead"
        )

    try:
        if paths.ENROLLMENT_PENDING.is_file():
            state = enroll_mod.resume(
                config_path=Path(args.config),
                token_file=paths.TOKEN_FILE,
                ca_path=Path(args.ca),
                recordings_root=args.root,
            )
            print(json.dumps(state, indent=2, sort_keys=True))
            return EXIT_OK

        recordings_root = args.root or _recordings_root_from_config(Path(args.config))
        if args.code_stdin:
            code = sys.stdin.read().strip()
        else:
            code = (args.code or "").strip()
        if not code:
            return _err("no enrollment code supplied (--code, or --code-stdin with the code on stdin)")
        state = enroll_mod.enroll(
            vault_url=args.vault,
            code=code,
            ca_path=Path(args.ca),
            config_path=Path(args.config),
            token_file=paths.TOKEN_FILE,
            recordings_root=recordings_root,
            agent_version=AGENT_VERSION,
            force=True,
            recovery_marker=paths.ENROLLMENT_PENDING,
            ingest=args.ingest,
        )
    except enroll_mod.EnrollmentError as exc:
        return _err(str(exc))
    except OSError as exc:
        return _err(f"could not complete re-enrollment: {exc}")

    print(json.dumps(state, indent=2, sort_keys=True))
    return EXIT_OK


# --------------------------------------------------------------------------
# run / status / logs
# --------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    return runner.main(["--config", args.config])


def cmd_status(args: argparse.Namespace) -> int:
    return status_mod.main(Path(args.config))


def cmd_logs(args: argparse.Namespace) -> int:
    cmd = ["journalctl", "-u", "ingotron-agent.service", "-n", str(args.lines)]
    if args.follow:
        cmd.append("-f")
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        return _err(f"could not run journalctl: {exc}")


# --------------------------------------------------------------------------
# credential rotate -- GATED ON self_rotation_enabled.
#
# `RotateCredential` is part of the enrollment design but is NOT implementable
# here: proto/enrollment.proto carries `Enroll` only, and no server implements
# the other two RPCs. The rule is not "refuse loudly forever" but
# "gate the command AND the timer behind the `self_rotation_enabled` flag the
# enrollment response already carries", so a robot enrolled against a server
# without the RPC installs no timer at all and says so, rather than retrying a
# missing method every week.
#
# So there are two distinct outcomes here, and conflating them would hide the
# difference between "this vault turned it off" and "this build cannot do
# it":
#   flag false  -> exit 0, "self-rotation: disabled by the vault". Nothing is
#                  wrong; robot.sh also installed no rotate timer.
#   flag true   -> exit 3, naming the missing RPC. That combination means the
#                  vault advertised self-rotation and the wire contract
#                  cannot deliver it, which an operator must see.
# --------------------------------------------------------------------------

SELF_ROTATION_DISABLED_MESSAGE = "self-rotation: disabled by the vault"


def _self_rotation_enabled() -> Optional[bool]:
    """The flag recorded at enrollment, or None if this robot is not enrolled."""
    try:
        state = json.loads(paths.ENROLLMENT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = state.get("self_rotation_enabled")
    return bool(value) if isinstance(value, bool) else None


def cmd_credential(args: argparse.Namespace) -> int:
    if args.credential_cmd != "rotate":
        return _err(f"unknown credential subcommand: {args.credential_cmd}")

    enabled = _self_rotation_enabled()
    if enabled is False:
        print(
            json.dumps(
                {
                    "rotated": False,
                    "reason": "self_rotation_disabled",
                    "detail": SELF_ROTATION_DISABLED_MESSAGE,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK
    if enabled is None:
        return _err(
            "this robot is not enrolled (no recorded enrollment state), so there is "
            "no credential to rotate"
        )

    print(
        json.dumps(
            {
                "rotated": False,
                "reason": "server_rpc_absent",
                "detail": (
                    "the vault advertised self_rotation_enabled=true, but "
                    "RobotEnrollment.RotateCredential is not present in "
                    "proto/enrollment.proto or in the ingest server, so there is "
                    "nothing to call. Rotate this robot from the vault instead: "
                    "mint a 'reenroll' code in the UI and run "
                    "`ingotron-agent enroll --force --vault <url> --code <code>` here."
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return EXIT_UNSUPPORTED


# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------

UNITS = (
    "ingotron-agent.timer",
    "ingotron-agent.service",
    "ingotron-agent-rotate.timer",
    "ingotron-agent-rotate.service",
    "ingotron-agent-retention.timer",
    "ingotron-agent-retention.service",
)


def _systemctl(*argv: str) -> int:
    try:
        return subprocess.call(["systemctl", *argv])
    except OSError:
        return 1


def _shred(path: Path) -> None:
    """Overwrite before unlinking, so the token does not linger in free blocks."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def cmd_uninstall(args: argparse.Namespace) -> int:
    guard = _require_root("uninstall")
    if guard is not None:
        return guard

    for unit in UNITS:
        if unit.endswith(".timer"):
            _systemctl("disable", "--now", unit)
        else:
            _systemctl("stop", unit)
    for unit in UNITS:
        try:
            (paths.UNIT_DIR / unit).unlink()
        except OSError:
            pass
    dropin = paths.UNIT_DIR / "ingotron-agent.service.d"
    if dropin.is_dir():
        shutil.rmtree(dropin, ignore_errors=True)
    _systemctl("daemon-reload")

    try:
        (Path("/usr/local/bin/ingotron-agent")).unlink()
    except OSError:
        pass

    removed_prefix = False
    if paths.AGENT_PREFIX.is_dir():
        shutil.rmtree(paths.AGENT_PREFIX, ignore_errors=True)
        removed_prefix = True

    result = {
        "units_removed": list(UNITS),
        "agent_prefix_removed": removed_prefix,
        "purged": bool(args.purge),
        "config_kept": not args.purge,
    }

    if args.purge:
        # --purge additionally shreds the token files and
        # best-effort calls Deregister. Deregister does not exist in the landed
        # enrollment service (see cmd_credential), so the honest behaviour is
        # the one already used for an unreachable vault: succeed
        # locally and print the exact server-side revoke command.
        # An interrupted enrollment's private recovery record contains the same
        # credential material as the token file. Purge must overwrite that copy
        # too before removing /etc/ingotron.
        for token in (paths.TOKEN_FILE, paths.TOKEN_FILE_PREV, paths.ENROLLMENT_PENDING):
            if token.is_file():
                _shred(token)
        enrollment = {}
        if paths.ENROLLMENT_STATE.is_file():
            try:
                enrollment = json.loads(paths.ENROLLMENT_STATE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                enrollment = {}
        shutil.rmtree(paths.CONF_DIR, ignore_errors=True)
        shutil.rmtree(paths.STATE_DIR, ignore_errors=True)
        result["deregistered"] = False
        result["deregister_reason"] = (
            "RobotEnrollment.Deregister is part of the enrollment design but is not present "
            "in the landed enrollment service; nothing was called."
        )
        robot_id = enrollment.get("robot_id", "<robot-id>")
        fingerprint = enrollment.get("credential_fingerprint", "<credential-id>")
        result["operator_action_required"] = (
            "Revoke this robot's credential on the vault: "
            f"POST /api/admin/robots/{robot_id}/credentials/{fingerprint}/revoke "
            f'{{"reason":"robot decommissioned"}} -- or disable the robot entirely: '
            f"POST /api/admin/robots/{robot_id}/disable"
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    return EXIT_OK


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingotron-agent")
    parser.add_argument("--version", action="version", version=AGENT_VERSION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enroll", help="enroll this robot against a vault")
    p.add_argument("--vault", required=True, help="https://<vault-host>")
    p.add_argument("--code", default=None, help="ign1.<pin>.<secret>")
    p.add_argument(
        "--ingest",
        default=None,
        help="ingest endpoint host:port for the exchange (default: <vault host>:50051)",
    )
    p.add_argument(
        "--code-stdin",
        action="store_true",
        help="read the code from stdin instead of argv (what robot.sh does)",
    )
    p.add_argument("--ca", default=str(paths.CA_CERT))
    p.add_argument("--config", default=str(paths.VAULT_CONFIG))
    p.add_argument("--token-file", default=str(paths.TOKEN_FILE))
    p.add_argument("--root", default="/data/robot_data")
    p.add_argument("--agent-version", default=None)
    p.add_argument("--result-file", default=None, help="KEY=VALUE summary for robot.sh")
    p.add_argument("--force", action="store_true", help="replace an existing enrollment")
    p.add_argument(
        "--resume",
        action="store_true",
        help="finish an enrollment whose exchange already succeeded, reusing the "
        "stored credential; needs no code",
    )
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("reenroll", help="replace this installed robot's credential")
    p.add_argument("--vault", required=True, help="https://<vault-host>")
    p.add_argument("--code", default=None, help="ign1.<pin>.<secret>")
    p.add_argument(
        "--ingest",
        default=None,
        help="ingest endpoint host:port for the exchange (default: <vault host>:50051)",
    )
    p.add_argument("--code-stdin", action="store_true", help="read the code from stdin")
    p.add_argument("--ca", default=str(paths.CA_CERT))
    p.add_argument("--config", default=str(paths.VAULT_CONFIG))
    p.add_argument(
        "--root",
        default=None,
        help="recordings tree; defaults to paths.robot_root in the installed config",
    )
    p.set_defaults(func=cmd_reenroll)

    p = sub.add_parser("run", help="one scan -> hash -> upload pass")
    p.add_argument("--config", default=str(paths.VAULT_CONFIG))
    p.add_argument("--once", action="store_true", help="accepted for symmetry; a run is one pass")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="what this robot is and what it last did")
    p.add_argument("--config", default=str(paths.VAULT_CONFIG))
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("logs", help="journalctl -u ingotron-agent.service")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("credential", help="credential operations")
    p.add_argument("credential_cmd", choices=["rotate"])
    p.add_argument("--if-older-than", default=None)
    p.set_defaults(func=cmd_credential)

    p = sub.add_parser("uninstall", help="remove the agent")
    p.add_argument(
        "--purge",
        action="store_true",
        help="also shred credentials and remove /etc/ingotron and /var/lib/ingotron",
    )
    p.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "__retention":
        # Private dispatch used by the frozen retention wrapper.  It is kept
        # out of argparse's operator-facing command list.
        from .retention import main as retention_main

        return int(retention_main(raw[1:]))
    if raw == ["__bundle-self-test"]:
        # Clean-host acceptance probe: force every dependency that enrollment
        # and upload need to import from the frozen PYZ/onedir, without opening
        # a socket or touching enrollment state.
        import grpc
        import enrollment_pb2
        import enrollment_pb2_grpc
        import upload_pb2
        import upload_pb2_grpc
        import prometheus_client
        import psutil
        import robot_storage_manager.cli
        import robot_upload_agent.cli
        import robot_vault_config

        print(
            json.dumps(
                {
                    "agent_version": AGENT_VERSION,
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "grpc": getattr(grpc, "__version__", "unknown"),
                    "imports": "ok",
                },
                sort_keys=True,
            )
        )
        return EXIT_OK
    parser = build_parser()
    args = parser.parse_args(raw)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
