"""The enrollment client -- bootstrap steps 6-8 and credential storage.

This module runs AFTER robot.sh has fetched the vault CA over the one
unverified request in the whole bootstrap and verified its SPKI pin against the
pin carried inside the enrollment code.  By the time `enroll()` is called the
CA at /etc/ingotron/ca.crt is known-good, and this module's entire job is:

  1. build a gRPC channel to <vault-host>:50051 with
     `ssl_channel_credentials(root_certificates=<that CA>)` and hostname
     verification ON -- there is no insecure fallback and no
     "connect and peek at the chain" path;
  2. wait for that channel to become READY *before* the request is serialised,
     so that an on-path attacker holding the genuine CA but not the ingest
     leaf's private key never receives the enrollment secret at all
     (a test covers this attack);
  3. exchange the one-use code for a per-robot credential;
  4. persist the credential (secretstore.py) and write a vault.json that references it
     by `auth.token_file` -- never inlined as `auth.token`, never placed in
     INGEST_AUTH_TOKEN, and never visible in `systemctl show`.

Constant-shape failure.  The server answers `UNAUTHENTICATED "invalid or
expired enrollment code"` for no-such-code, expired, revoked and
already-consumed alike (verified against phases/03-ingest-server-rs/src/
enrollment.rs, ENROLL_FAILURE_MESSAGE).  This client does not try to guess
which; it reports what the server said and what the operator can do next.
"""

import hashlib
import hmac
import json
import math
import os
import socket
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import AGENT_VERSION, paths, secretstore

PLATFORM = "linux/amd64"

#: Enrollment code: `ign1.<26>.<32>` = 64 characters.
CODE_LENGTH = 64
CODE_PREFIX = "ign1."
CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

#: Per-robot credential: `ignr1.<52 chars>`.
CREDENTIAL_PREFIX = "ignr1."
CREDENTIAL_LENGTH = 58

CONNECT_TIMEOUT_SECONDS = 20.0
RPC_TIMEOUT_SECONDS = 30.0

PENDING_SCHEMA_VERSION = 1
PENDING_MODE = 0o600
PENDING_DIR_MODE = 0o700
_PENDING_MAX_BYTES = 32 * 1024
_PENDING_STATE_KEYS = {
    "robot_id",
    "credential_fingerprint",
    "ingest_endpoint",
    "vault_url",
    "enrolled_at",
    "self_rotation_enabled",
    "agent_version",
    "recommended",
    "recordings_root",
}


class EnrollmentError(RuntimeError):
    """Anything that stops an enrollment.  Always fatal, never retried in-process."""


def parse_code(code: str) -> Dict[str, str]:
    """Validate an `ign1.` code and split it into its two segments.

    robot.sh already validated this before it opened any socket; the agent
    re-validates because `ingotron-agent enroll` is also a supported direct
    entry point and must not be the weaker of the two.
    """
    if len(code) != CODE_LENGTH:
        raise EnrollmentError(
            f"enrollment code must be {CODE_LENGTH} characters, got {len(code)}"
        )
    if not code.startswith(CODE_PREFIX):
        raise EnrollmentError(
            "enrollment code must begin with 'ign1.'; a code with any other "
            "version tag is rejected before any lookup"
        )
    rest = code[len(CODE_PREFIX):]
    if rest.count(".") != 1:
        raise EnrollmentError("enrollment code must be ign1.<pin>.<secret>")
    pin, secret = rest.split(".", 1)
    if len(pin) != 26:
        raise EnrollmentError(f"enrollment code pin segment must be 26 characters, got {len(pin)}")
    if len(secret) != 32:
        raise EnrollmentError(
            f"enrollment code secret segment must be 32 characters, got {len(secret)}"
        )
    for label, segment in (("pin", pin), ("secret", secret)):
        if not set(segment) <= CROCKFORD:
            raise EnrollmentError(
                f"enrollment code {label} segment contains a character outside "
                "the Crockford base32 alphabet"
            )
    return {"pin": pin, "secret": secret}


def parse_vault_url(url: str) -> Dict[str, Any]:
    if not url.startswith("https://"):
        raise EnrollmentError("--vault must be an https:// URL")
    hostport = url[len("https://"):].rstrip("/")
    if "/" in hostport or "@" in hostport:
        raise EnrollmentError("--vault must be https://host[:port] with no path or credentials")
    if ":" in hostport:
        host, _, port = hostport.rpartition(":")
        if not port.isdigit():
            raise EnrollmentError("--vault port must be numeric")
        return {"host": host, "port": int(port)}
    return {"host": hostport, "port": 443}


_HOST_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-")


def parse_ingest(endpoint: Optional[str], vault_host: str) -> Dict[str, Any]:
    """`--ingest host:port`, or `<vault host>:50051` when it is not given.

    The query and ingest services can have different public names, so the
    install command passes the endpoint the vault returned with the code.
    """
    if endpoint is None:
        return {"host": vault_host, "port": paths.ENROLLMENT_PORT}
    host, sep, port = endpoint.rpartition(":")
    if not sep or not host or not set(host) <= _HOST_CHARS:
        raise EnrollmentError(
            "--ingest must be host:port; the host may contain only letters, digits, '.' and '-'"
        )
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise EnrollmentError("--ingest port must be a number from 1 to 65535")
    return {"host": host, "port": int(port)}


def validate_recordings_root(root: str) -> str:
    """Validate the recording tree before any enrollment side effect.

    The root is emitted into a shell command and a systemd ReadWritePaths=
    drop-in. ASCII whitespace is therefore forbidden: systemd would split a
    value such as ``/srv/recordings /etc`` into two writable paths.
    """
    if not isinstance(root, str) or not root.startswith("/"):
        raise EnrollmentError("recordings root must be an absolute path")
    if root == "/" or root.endswith("/") or "//" in root or ".." in root.split("/"):
        raise EnrollmentError("recordings root must be a normalised absolute path")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in root):
        raise EnrollmentError(
            "recordings root may not contain ASCII whitespace or control characters"
        )
    if any(char in root for char in "'\"$`\\%"):
        raise EnrollmentError("recordings root contains shell or systemd metacharacters")
    denied = (
        "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
        "/proc", "/sys", "/dev", "/run", "/var/lib/ingotron", "/opt/ingotron-agent",
    )
    if any(root == prefix or root.startswith(prefix + "/") for prefix in denied):
        raise EnrollmentError("recordings root is inside a system-owned path")
    return root


def _load_stubs():
    """Import the generated enrollment stubs, with an actionable error if absent.

    The pinned builder generates the stubs inside its container and embeds them
    in the frozen PYZ. Source-checkout tests generate them in a temporary import
    directory; there is no shipped source-tree fallback.
    """
    try:
        import enrollment_pb2  # type: ignore
        import enrollment_pb2_grpc  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by the live gate
        raise EnrollmentError(
            "the generated enrollment stubs are missing; a frozen release with "
            "this error is invalid. Source-checkout tests can generate them with: "
            "python -m grpc_tools.protoc -I proto --python_out=<dir> "
            "--grpc_python_out=<dir> proto/upload.proto proto/enrollment.proto"
        ) from exc
    return enrollment_pb2, enrollment_pb2_grpc


def exchange_code(
    *,
    host: str,
    code: str,
    ca_path: Path,
    port: int = paths.ENROLLMENT_PORT,
    agent_version: str = AGENT_VERSION,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    rpc_timeout: float = RPC_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Run RobotEnrollment.Enroll and return the response as a plain dict."""
    import grpc  # lazy so status does not initialize the enrollment transport

    enrollment_pb2, enrollment_pb2_grpc = _load_stubs()

    ca_bytes = Path(ca_path).read_bytes()
    if b"BEGIN CERTIFICATE" not in ca_bytes:
        raise EnrollmentError(f"{ca_path} does not look like a PEM certificate")

    # ssl_channel_credentials, always. There is no insecure_channel on this
    # path and no environment variable that turns verification off.
    credentials = grpc.ssl_channel_credentials(root_certificates=ca_bytes)
    target = f"{host}:{port}"
    channel = grpc.secure_channel(target, credentials)
    try:
        # Block on the handshake BEFORE the request exists as bytes. An on-path
        # attacker who replayed the genuine CA still does not hold the ingest
        # leaf's private key, so the channel never reaches READY and the code
        # is never serialised onto the wire.
        try:
            grpc.channel_ready_future(channel).result(timeout=connect_timeout)
        except grpc.FutureTimeoutError as exc:
            raise EnrollmentError(
                f"could not establish a verified TLS connection to {target} within "
                f"{connect_timeout:.0f}s. The enrollment code was NOT sent. Check that "
                f"the vault's ingest port is reachable and that its certificate "
                f"chains to {ca_path}."
            ) from exc

        stub = enrollment_pb2_grpc.RobotEnrollmentStub(channel)
        request = enrollment_pb2.EnrollRequest(
            code=code,
            agent_version=agent_version,
            hostname=socket.gethostname(),
            platform=PLATFORM,
        )
        try:
            response = stub.Enroll(request, timeout=rpc_timeout)
        except grpc.RpcError as exc:  # pragma: no cover - covered by the live gate
            code_name = getattr(exc, "code", lambda: None)()
            detail = getattr(exc, "details", lambda: str(exc))()
            if code_name is not None and code_name == grpc.StatusCode.UNAUTHENTICATED:
                raise EnrollmentError(
                    f"the vault rejected the enrollment code: {detail}. "
                    "The vault answers identically for an unknown, expired, "
                    "revoked or already-used code; mint a fresh code in the UI "
                    "and re-run."
                ) from exc
            if code_name is not None and code_name == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise EnrollmentError(
                    f"the vault is rate-limiting enrollment: {detail}. Wait and re-run."
                ) from exc
            raise EnrollmentError(f"enrollment RPC failed ({code_name}): {detail}") from exc
    finally:
        channel.close()

    recommended = response.recommended
    return {
        "robot_id": response.robot_id,
        "credential": response.credential,
        "credential_fingerprint": response.credential_fingerprint,
        "ingest_endpoint": response.ingest_endpoint,
        "supersedes_grace_seconds": int(response.supersedes_grace_seconds),
        "self_rotation_enabled": bool(response.self_rotation_enabled),
        "recommended": {
            "chunk_size_bytes": int(recommended.chunk_size_bytes),
            "parallel_streams": int(recommended.parallel_streams),
            "min_stable_age_seconds": float(recommended.min_stable_age_seconds),
        },
    }


def validate_credential(credential: str) -> None:
    if not isinstance(credential, str):
        raise EnrollmentError("the vault returned a non-text credential")
    if not credential.startswith(CREDENTIAL_PREFIX):
        raise EnrollmentError(
            "the vault returned a credential that is not an 'ignr1.' per-robot token"
        )
    if len(credential) != CREDENTIAL_LENGTH:
        raise EnrollmentError(
            f"the vault returned a credential of length {len(credential)}; "
            f"the credential format fixes it at {CREDENTIAL_LENGTH}"
        )
    try:
        credential.encode("ascii")
    except UnicodeEncodeError as exc:
        raise EnrollmentError("the vault returned a non-ASCII credential") from exc
    if credential != credential.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in credential):
        raise EnrollmentError("the vault returned a credential with whitespace or control characters")


def split_endpoint(endpoint: str) -> Dict[str, Any]:
    """`host:port` -> {host, port}. The endpoint comes from INGEST_PUBLIC_HOST."""
    if ":" not in endpoint:
        return {"host": endpoint, "port": paths.ENROLLMENT_PORT}
    host, _, port = endpoint.rpartition(":")
    if not port.isdigit():
        raise EnrollmentError(f"the vault returned a malformed ingest endpoint: {endpoint}")
    return {"host": host, "port": int(port)}


def build_vault_config(
    *,
    robot_id: str,
    ingest_endpoint: str,
    recordings_root: str,
    ca_path: Path,
    token_file: Path,
    recommended: Dict[str, Any],
) -> Dict[str, Any]:
    """The vault.json robot.sh installs (bootstrap step 8).

    `tls.enabled` is the only TLS switch; `cert_path`/`key_path` are absent
    because `mtls` is false; and the credential is referenced by
    `auth.token_file`, not inlined.
    """
    recordings_root = validate_recordings_root(recordings_root)
    endpoint = split_endpoint(ingest_endpoint)
    upload_agent: Dict[str, Any] = {}
    if recommended.get("chunk_size_bytes"):
        upload_agent["chunk_size_bytes"] = int(recommended["chunk_size_bytes"])
    if recommended.get("parallel_streams"):
        upload_agent["parallel_streams"] = int(recommended["parallel_streams"])
        upload_agent["concurrency"] = int(recommended["parallel_streams"])
    if recommended.get("min_stable_age_seconds"):
        upload_agent["min_stable_age_seconds"] = float(recommended["min_stable_age_seconds"])

    config: Dict[str, Any] = {
        "robot_id": robot_id,
        "paths": {"robot_root": recordings_root},
        "server": {"host": endpoint["host"], "port": endpoint["port"]},
        "tls": {"enabled": True, "mtls": False, "ca_path": str(ca_path)},
        "auth": {"token_file": str(token_file)},
    }
    if upload_agent:
        config["upload_agent"] = upload_agent
    return config


def write_vault_config(path: Path, config: Dict[str, Any]) -> None:
    """Write vault.json 0644 root:root, then prove it loads.

    The loader is strict about unknown keys, so a typo here would only surface
    at the first timer run.  Loading it back turns that into an enrollment-time
    failure with the credential already safely stored.
    """
    _atomic_write(
        path,
        (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        0o644,
    )

    try:
        from robot_vault_config.model import load_config
    except ImportError:  # pragma: no cover - the bundle always ships it
        return
    loaded = load_config(path)
    loaded.validate_tls()
    if getattr(loaded.auth, "token_file", None) != str(config["auth"]["token_file"]):
        raise EnrollmentError(
            "the vault.json just written does not round-trip auth.token_file; the "
            "installed robot_vault_config is older than this agent"
        )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    """Atomically replace `path` and make both file and name durable.

    A stale `.new` can only be a prior interrupted write. Removing it before an
    `O_EXCL` create means an attacker cannot make us follow it; in the recovery
    directory the parent is root-only, and in the public state directory this
    still turns a hostile race into a failure rather than a write through a
    symlink.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(path) + ".new")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(
        str(temporary),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while persisting enrollment data")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(temporary), str(path))
    _fsync_directory(path.parent)


def _ensure_private_recovery_parent(path: Path) -> None:
    """Make the recovery directory private to the root-run enrollment command."""
    path.parent.mkdir(parents=True, exist_ok=True)
    details = path.parent.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise EnrollmentError(f"could not secure the enrollment recovery directory {path.parent}")
    os.chmod(str(path.parent), PENDING_DIR_MODE)
    _fsync_directory(path.parent.parent)
    _validate_private_recovery_parent(path)
    _fsync_directory(path.parent)


def _validate_private_recovery_parent(path: Path) -> None:
    details = path.parent.lstat()
    if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != PENDING_DIR_MODE:
        raise EnrollmentError(f"could not secure the enrollment recovery directory {path.parent}")
    if details.st_uid != os.getuid():
        raise EnrollmentError(f"the enrollment recovery directory {path.parent} is not owned by this command")


def _credential_digest(credential: str) -> str:
    return hashlib.sha256(credential.encode("ascii")).hexdigest()


def _validate_text_field(value: Any, name: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise EnrollmentError(f"the enrollment recovery record has an invalid {name}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise EnrollmentError(f"the enrollment recovery record has an invalid {name}")
    return value


def _validate_state(state: Any, *, pending: bool) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise EnrollmentError("the enrollment recovery record has no valid state")
    if pending and set(state) != _PENDING_STATE_KEYS:
        raise EnrollmentError("the enrollment recovery record has an unexpected state schema")
    for field in ("robot_id", "credential_fingerprint", "ingest_endpoint"):
        _validate_text_field(state.get(field), field)
    endpoint = split_endpoint(state["ingest_endpoint"])
    if not endpoint["host"]:
        raise EnrollmentError("the enrollment recovery record has an invalid ingest_endpoint")
    if not isinstance(state.get("self_rotation_enabled"), bool):
        raise EnrollmentError("the enrollment recovery record has an invalid self_rotation_enabled flag")
    if "recommended" in state:
        recommended = state["recommended"]
        if not isinstance(recommended, dict) or set(recommended) - {
            "chunk_size_bytes", "parallel_streams", "min_stable_age_seconds"
        }:
            raise EnrollmentError("the enrollment recovery record has invalid recommended settings")
        for field in ("chunk_size_bytes", "parallel_streams"):
            if field in recommended and (
                type(recommended[field]) is not int or recommended[field] < 0
            ):
                raise EnrollmentError("the enrollment recovery record has invalid recommended settings")
        if "min_stable_age_seconds" in recommended and (
            not isinstance(recommended["min_stable_age_seconds"], (int, float))
            or isinstance(recommended["min_stable_age_seconds"], bool)
            or not math.isfinite(recommended["min_stable_age_seconds"])
            or recommended["min_stable_age_seconds"] < 0
        ):
            raise EnrollmentError("the enrollment recovery record has invalid recommended settings")
    if pending:
        parse_vault_url(_validate_text_field(state["vault_url"], "vault_url"))
        _validate_text_field(state["enrolled_at"], "enrolled_at", limit=64)
        _validate_text_field(state["agent_version"], "agent_version", limit=128)
        validate_recordings_root(state["recordings_root"])
    return dict(state)


def _write_recovery_marker(path: Path, state: Dict[str, Any], credential: str) -> None:
    """Persist all post-exchange recovery data before replacing a live token.

    This is deliberately root-only and includes the credential because the
    enrollment code is already consumed. The public enrollment state remains
    secret-free and is never the source of truth while this record exists.
    """
    _ensure_private_recovery_parent(path)
    checked_state = _validate_state(state, pending=True)
    validate_credential(credential)
    payload = {
        "version": PENDING_SCHEMA_VERSION,
        "credential": credential,
        "credential_sha256": _credential_digest(credential),
        "state": checked_state,
    }
    _atomic_write(
        path,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        PENDING_MODE,
    )


def _read_recovery_marker(path: Path) -> Tuple[Dict[str, Any], str]:
    try:
        _validate_private_recovery_parent(path)
        details = path.stat()
        if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != PENDING_MODE:
            raise EnrollmentError("the enrollment recovery record has unsafe permissions")
        if details.st_uid != os.getuid():
            raise EnrollmentError("the enrollment recovery record is not owned by this command")
        raw = path.read_bytes()
    except OSError as exc:
        raise EnrollmentError("could not read the enrollment recovery record") from exc
    if not raw or len(raw) > _PENDING_MAX_BYTES:
        raise EnrollmentError("the enrollment recovery record has an invalid size")
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnrollmentError("the enrollment recovery record is corrupt") from exc
    if not isinstance(marker, dict) or set(marker) != {
        "version", "credential", "credential_sha256", "state"
    }:
        raise EnrollmentError("the enrollment recovery record has an unexpected schema")
    if type(marker["version"]) is not int or marker["version"] != PENDING_SCHEMA_VERSION:
        raise EnrollmentError("the enrollment recovery record has an unsupported version")
    credential = marker["credential"]
    if not isinstance(credential, str):
        raise EnrollmentError("the enrollment recovery record has an invalid credential")
    validate_credential(credential)
    fingerprint = marker["credential_sha256"]
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise EnrollmentError("the enrollment recovery record has an invalid credential fingerprint")
    try:
        int(fingerprint, 16)
    except ValueError as exc:
        raise EnrollmentError("the enrollment recovery record has an invalid credential fingerprint") from exc
    if not hmac.compare_digest(_credential_digest(credential), fingerprint):
        raise EnrollmentError("the enrollment recovery record credential fingerprint does not match")
    return _validate_state(marker["state"], pending=True), credential


def clear_recovery_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def enroll(
    *,
    vault_url: str,
    code: str,
    ca_path: Path,
    config_path: Path,
    token_file: Path,
    recordings_root: str,
    agent_version: str = AGENT_VERSION,
    result_file: Optional[Path] = None,
    force: bool = False,
    recovery_marker: Optional[Path] = None,
    ingest: Optional[str] = None,
) -> Dict[str, Any]:
    recordings_root = validate_recordings_root(recordings_root)
    parse_code(code)
    vault = parse_vault_url(vault_url)
    target = parse_ingest(ingest, vault["host"])

    if token_file.exists() and not force:
        raise EnrollmentError(
            f"{token_file} already exists: this robot is already enrolled. "
            "Re-run with --force to replace the credential (the old one stays "
            "valid on the vault until you revoke it)."
        )

    # The record is written before the token replacement, whether this is a
    # first enrollment or a re-enrollment. After an exchange the code is spent,
    # so normal enrollment state alone is not a sufficient recovery boundary.
    recovery_marker = recovery_marker or paths.ENROLLMENT_PENDING
    if recovery_marker.exists():
        raise EnrollmentError(
            "an enrollment recovery record already exists; resume the interrupted "
            "enrollment instead of sending another one-use code"
        )
    _ensure_private_recovery_parent(recovery_marker)

    response = exchange_code(
        host=target["host"],
        port=target["port"],
        code=code,
        ca_path=ca_path,
        agent_version=agent_version,
    )
    validate_credential(response["credential"])

    state = {
        "robot_id": response["robot_id"],
        "credential_fingerprint": response["credential_fingerprint"],
        # BOTH endpoints. The ingest endpoint comes from the vault and is
        # where uploads go; the vault URL is the query origin the operator
        # typed, and it is the only place it is recorded -- nothing else on the
        # robot remembers it, and `status` is required to print it.
        "ingest_endpoint": response["ingest_endpoint"],
        "vault_url": vault_url,
        "enrolled_at": _utc_now(),
        "self_rotation_enabled": response["self_rotation_enabled"],
        "agent_version": agent_version,
        "recommended": response["recommended"],
        "recordings_root": recordings_root,
    }
    try:
        _write_recovery_marker(recovery_marker, state, response["credential"])
    except (OSError, EnrollmentError) as exc:
        raise EnrollmentError(
            "the vault accepted the enrollment code but this host could not "
            "durably record recovery data before changing its credential. The code "
            "cannot be retried; repair local storage and mint a fresh code."
        ) from exc

    try:
        uid, gid = secretstore.resolve_service_ids(paths.SERVICE_USER, paths.SERVICE_GROUP)
        secretstore.write_secret(
            token_file,
            response["credential"],
            uid=uid,
            gid=gid,
            replace=force and token_file.exists(),
        )
        return _finish_pending_enrollment(
            recovery_marker=recovery_marker,
            config_path=config_path,
            token_file=token_file,
            ca_path=ca_path,
            recordings_root=recordings_root,
            result_file=result_file,
            token_already_checked=True,
        )
    except (OSError, EnrollmentError) as exc:
        raise EnrollmentError(
            "the enrollment exchange succeeded and the replacement credential is "
            "preserved in a private recovery record. It may also have reached the "
            f"credential file at {token_file}. Do NOT mint a new code; re-run the "
            "same command to finish."
        ) from exc


def resume(
    *,
    config_path: Path,
    token_file: Path,
    ca_path: Path,
    recordings_root: Optional[str],
    result_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Finish an enrollment whose EXCHANGE already succeeded.

    This is the path robot.sh step 7 promises when it says a re-run does not
    need a new code. The enrollment code is one-use and was spent when the
    server committed, so there is nothing left to exchange. The private pending
    record is the authority until token, public state and config are durable.

    Without this, a robot whose recordings tree happened to be unwritable ends
    up holding a valid credential, no units, and no supported way forward: the
    one-liner refuses over the token and `enroll --force` would need a fresh
    code. That is a bricked install produced by a chmod.
    """
    if paths.ENROLLMENT_PENDING.is_file():
        return _finish_pending_enrollment(
            recovery_marker=paths.ENROLLMENT_PENDING,
            config_path=config_path,
            token_file=token_file,
            ca_path=ca_path,
            recordings_root=recordings_root,
            result_file=result_file,
        )

    if not token_file.is_file():
        raise EnrollmentError(
            f"--resume needs an already-stored credential at {token_file}, and there "
            "is none. This robot has not completed an exchange; run the enrollment "
            "command from the Robot Vault UI instead."
        )
    try:
        state = json.loads(paths.ENROLLMENT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EnrollmentError(
            f"--resume needs the facts recorded at {paths.ENROLLMENT_STATE} and could "
            f"not read them ({exc}). The credential at {token_file} is still valid; "
            "mint a 'reenroll' code in the Robot Vault UI and run "
            "`ingotron-agent enroll --force` to replace it."
        ) from exc

    state = _validate_state(state, pending=False)
    if recordings_root is None:
        recordings_root = state.get("recordings_root")
    recordings_root = validate_recordings_root(recordings_root)

    # The recordings root may legitimately differ from the recorded one: a
    # re-run is often exactly how an operator corrects it.
    state["recordings_root"] = recordings_root

    write_vault_config(
        config_path,
        build_vault_config(
            robot_id=state["robot_id"],
            ingest_endpoint=state["ingest_endpoint"],
            recordings_root=recordings_root,
            ca_path=ca_path,
            token_file=token_file,
            recommended=state.get("recommended") or {},
        ),
    )
    _write_state(state)
    if result_file is not None:
        _write_result_file(result_file, state)
    return state


def _utc_now() -> str:
    """RFC3339 UTC. `CredentialResponse` carries no issued_at, so the moment the
    robot received the credential is the only issue time it can know."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_state(state: Dict[str, Any]) -> None:
    _validate_state(state, pending=False)
    _atomic_write(
        paths.ENROLLMENT_STATE,
        (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        0o644,
    )


def _write_result_file(path: Path, state: Dict[str, Any]) -> None:
    """A flat KEY=VALUE file for robot.sh, which has no JSON parser.

    Carries no secret: the credential is on disk at token_file, and the
    fingerprint is the non-secret handle for it.
    """
    lines = [
        f"robot_id={state['robot_id']}",
        f"credential_fingerprint={state['credential_fingerprint']}",
        f"ingest_endpoint={state['ingest_endpoint']}",
        "self_rotation_enabled=" + ("true" if state["self_rotation_enabled"] else "false"),
    ]
    _atomic_write(path, ("\n".join(lines) + "\n").encode("utf-8"), 0o600)


def _token_digest(path: Path) -> Optional[str]:
    try:
        credential = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    if credential != credential.strip():
        return None
    try:
        validate_credential(credential)
    except EnrollmentError:
        return None
    return _credential_digest(credential)


def _finish_pending_enrollment(
    *,
    recovery_marker: Path,
    config_path: Path,
    token_file: Path,
    ca_path: Path,
    recordings_root: Optional[str],
    result_file: Optional[Path],
    token_already_checked: bool = False,
) -> Dict[str, Any]:
    """Finish a durable post-exchange transaction without another RPC."""
    state, credential = _read_recovery_marker(recovery_marker)
    expected_digest = _credential_digest(credential)
    if recordings_root is None:
        recordings_root = state["recordings_root"]
    recordings_root = validate_recordings_root(recordings_root)
    if state["recordings_root"] != recordings_root:
        state["recordings_root"] = recordings_root
        _write_recovery_marker(recovery_marker, state, credential)

    actual_digest = _token_digest(token_file)
    if not token_already_checked or actual_digest != expected_digest:
        uid, gid = secretstore.resolve_service_ids(paths.SERVICE_USER, paths.SERVICE_GROUP)
        secretstore.write_secret(
            token_file,
            credential,
            uid=uid,
            gid=gid,
            replace=token_file.exists(),
        )
        if _token_digest(token_file) != expected_digest:
            raise EnrollmentError("could not verify the recovered credential on disk")

    _write_state(state)
    write_vault_config(
        config_path,
        build_vault_config(
            robot_id=state["robot_id"],
            ingest_endpoint=state["ingest_endpoint"],
            recordings_root=recordings_root,
            ca_path=ca_path,
            token_file=token_file,
            recommended=state.get("recommended") or {},
        ),
    )
    if result_file is not None:
        _write_result_file(result_file, state)
    clear_recovery_marker(recovery_marker)
    # The credential is deliberately NOT in the returned dict: callers print it.
    return state
