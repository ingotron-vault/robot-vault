"""Admin-gated proxy for ingest's private robot-provisioning API.

Ingest owns robot lifecycle writes and is intentionally reachable only on the
Compose network. This router is the single browser-facing bridge: it accepts
an administrator's OIDC session, adds the narrowly scoped provisioning token,
and forwards only an allowlist of provisioning routes to ``http://ingest:8080``. It never
forwards the browser bearer token, cookies, arbitrary headers, or an arbitrary
upstream URL.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote as url_quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import auth, robot_bootstrap, settings

logger = logging.getLogger(__name__)

router = APIRouter()

_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 10.0
# The provisioning API has deliberately small JSON replies.  Bound this
# separately from the browser request limit: the upstream is private but is
# still a process boundary, so it must not be able to make query buffer an
# unbounded response in memory.
_MAX_UPSTREAM_RESPONSE_BYTES = 16 * 1024 * 1024
_ACTION_STATES = {
    "disable": "disabled",
    # Ingest's state machine restores a disabled pending robot to pending when
    # it receives the requested enrolled state; the proxy must not reimplement
    # that lifecycle decision.
    "enable": "enrolled",
    "decommission": "decommissioned",
}
_RECORDING_ROOT_DENIED_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/lib/ingotron",
    "/opt/ingotron-agent",
)


def _unavailable() -> JSONResponse:
    # One deliberately dull public error for missing configuration, a failed
    # connection, timeout, malformed internal response, and an ingest 5xx.
    # In particular, never return a mint body or a token/configuration detail.
    # Do not use HTTPException here: FastAPI would turn it into the generic
    # ``{"detail": ...}`` envelope, whereas this API's public failure
    # contract is ``{"error": "provisioning_unavailable"}``.
    return JSONResponse(
        status_code=502,
        content={"error": "provisioning_unavailable"},
    )


def _actor_subject(user: object) -> Optional[str]:
    """Return a header-safe, bounded OIDC subject for ingest audit metadata."""
    value = getattr(user, "sub", None)
    if not isinstance(value, str):
        return None
    # Header values must be printable ASCII. Ingest treats this as untrusted
    # display metadata too, but normalizing before httpx sees it avoids a
    # malformed OIDC subject causing an accidental request failure.
    value = "".join(char for char in value if 0x21 <= ord(char) <= 0x7E)[:255]
    return value or None


def _upstream_path(*segments: str) -> str:
    """Encode path parameters once; no user input can alter the allowlist."""
    return "/admin/v2/robots" + "".join(
        "/" + url_quote(segment, safe="") for segment in segments
    )


async def _proxy(
    *,
    method: str,
    path: str,
    user: object,
    payload: Optional[bytes] = None,
) -> Response:
    """Pass one allowlisted request through, preserving valid upstream replies."""
    if not settings.provisioning_token_configured():
        return _unavailable()
    try:
        upstream = settings.provisioning_upstream_url()
    except ValueError:
        logger.error("Provisioning upstream configuration is invalid")
        return _unavailable()

    headers: Dict[str, str] = {
        "accept": "application/json",
        "authorization": "Bearer " + str(settings.INGEST_PROVISIONING_TOKEN),
    }
    actor = _actor_subject(user)
    if actor:
        headers["x-ingotron-actor"] = actor
    if payload:
        # The ingress contract is JSON. Never forward an attacker-controlled
        # content type, which could make an otherwise JSON action mean
        # something different to a future upstream.
        headers["content-type"] = "application/json"

    timeout = httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS,
        read=_READ_TIMEOUT_SECONDS,
        write=_READ_TIMEOUT_SECONDS,
        pool=_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            request = client.build_request(
                method,
                upstream + path,
                headers=headers,
                content=payload,
            )
            upstream_response = await client.send(request, stream=True)
            try:
                # Query has already authenticated an administrator. An upstream
                # 401/403 is therefore a broken internal credential, not a browser
                # authorization result; redirects are equally invalid on this fixed
                # private API. Keep both configuration details out of the
                # browser-facing response.
                if (
                    upstream_response.status_code < 200
                    or 300 <= upstream_response.status_code < 400
                    or upstream_response.status_code in {401, 403}
                    or upstream_response.status_code >= 500
                ):
                    logger.warning(
                        "Provisioning upstream returned HTTP %d",
                        upstream_response.status_code,
                    )
                    return _unavailable()

                content = await _read_bounded_response(upstream_response)
                content_type = upstream_response.headers.get("content-type")
            finally:
                await upstream_response.aclose()
    except httpx.HTTPError as exc:
        logger.warning("Provisioning upstream failed (%s)", type(exc).__name__)
        return _unavailable()
    except _ResponseTooLarge:
        logger.warning(
            "Provisioning upstream response exceeded the %d byte cap",
            _MAX_UPSTREAM_RESPONSE_BYTES,
        )
        return _unavailable()
    except Exception as exc:
        # Header encoding and malformed internal URLs are not necessarily
        # subclasses of HTTPError. They still must not leak credentials or
        # become a successful-looking browser reply.
        logger.error("Provisioning proxy failed (%s)", type(exc).__name__)
        return _unavailable()

    response_headers = {"content-type": content_type} if content_type else None
    return Response(
        content=content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


class _ResponseTooLarge(ValueError):
    """The private upstream violated the bounded response contract."""


async def _read_bounded_response(response: httpx.Response) -> bytes:
    """Read an allowed reply without trusting a private service's body size.

    Content-Length is a cheap fail-fast check.  The byte count remains
    authoritative because an upstream may omit, understate, or stream the
    header.  The caller always closes ``response`` in a ``finally`` block.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise _ResponseTooLarge("invalid Content-Length") from exc
        if declared_size < 0 or declared_size > _MAX_UPSTREAM_RESPONSE_BYTES:
            raise _ResponseTooLarge("declared body exceeds cap")

    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > _MAX_UPSTREAM_RESPONSE_BYTES:
            raise _ResponseTooLarge("streamed body exceeds cap")
        body.extend(chunk)
    return bytes(body)


async def _request_body(request: Request) -> bytes:
    """Read a JSON route body without inheriting browser request headers."""
    return await request.body()


def _validate_recording_root(value: object) -> str:
    """Match robot.sh's portable `--root` preflight before rendering a shell command."""
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="invalid_recording_root")
    root = value
    if (
        not root
        or not root.startswith("/")
        or root == "/"
        or root.endswith("/")
        or "//" in root
        or ".." in root.split("/")
    ):
        raise HTTPException(status_code=422, detail="invalid_recording_root")
    if len(root) > 4096 or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in root):
        raise HTTPException(status_code=422, detail="invalid_recording_root")
    # These are rejected by robot.sh too. Reject them here rather than placing
    # a value that its systemd drop-in or shell probe will refuse into a command.
    if any(char in root for char in "'\"$`\\%"):
        raise HTTPException(status_code=422, detail="invalid_recording_root")
    if any(root == prefix or root.startswith(prefix + "/") for prefix in _RECORDING_ROOT_DENIED_PREFIXES):
        raise HTTPException(status_code=422, detail="invalid_recording_root")
    return root


async def _mint_request_payload(request: Request) -> tuple[bytes, Optional[str], Optional[str]]:
    """Strip the query-only recording root before forwarding mint data to ingest."""
    body = await _request_body(request)
    try:
        supplied = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        # Keep the established proxy behavior for malformed or future payloads.
        # Ingest remains the authority for those requests.
        return body, None, None
    if not isinstance(supplied, dict):
        return body, None, None

    purpose = supplied.get("purpose") if supplied.get("purpose") == "reenroll" else None
    if "recording_root" not in supplied:
        return body, None, purpose

    recording_root = _validate_recording_root(supplied.pop("recording_root"))
    return json.dumps(supplied, separators=(",", ":")).encode("utf-8"), recording_root, purpose


@router.post("/api/admin/robots")
async def create_robot(
    request: Request,
    user=Depends(auth.require_role("admin")),
) -> Response:
    return await _proxy(
        method="POST",
        path=_upstream_path(),
        user=user,
        payload=await _request_body(request),
    )


@router.get("/api/admin/robots/lifecycle")
async def robot_lifecycle(
    user=Depends(auth.require_role("admin")),
) -> Response:
    return await _proxy(
        method="GET",
        path=_upstream_path(),
        user=user,
    )


@router.post("/api/admin/robots/{robot_id}/enrollment-code")
async def mint_enrollment_code(
    robot_id: str,
    request: Request,
    user=Depends(auth.require_role("admin")),
) -> Response:
    payload, recording_root, purpose = await _mint_request_payload(request)
    response = await _proxy(
        method="POST",
        path=_upstream_path(robot_id, "enrollment-codes"),
        user=user,
        payload=payload,
    )
    if response.status_code != 201:
        return response
    # `response` is an upstream JSON reply and therefore may be malformed in
    # the failure mode. Do not fabricate a successful mint when it is.
    try:
        decoded: Any = json.loads(response.body)
    except (TypeError, ValueError, UnicodeDecodeError):
        decoded = None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("code"), str) or not decoded["code"]:
        logger.error("Provisioning mint response had no usable code")
        return _unavailable()
    minted: Dict[str, Any] = decoded
    # From here on ingest has spent a one-use code: always return it, with the
    # metadata a client needs to build the command itself.
    minted["install_mode"] = "reenroll" if purpose == "reenroll" else "install"
    if recording_root is not None:
        minted["recording_root"] = recording_root
    script_sha256 = None
    try:
        script_sha256 = robot_bootstrap.script_sha256(robot_bootstrap.read_script())
        vault = robot_bootstrap.vault_url()
        endpoint = minted.get("vault_ingest_endpoint")
        # Same rule as the UI fallback: no valid ingest host:port, no command.
        if script_sha256 and vault and robot_bootstrap.valid_ingest_endpoint(endpoint):
            minted["install_command"] = robot_bootstrap.install_command(
                vault=vault,
                script_sha256=script_sha256,
                code=minted["code"],
                reenroll=purpose == "reenroll",
                ingest_endpoint=endpoint,
                recording_root=recording_root,
            )
    except Exception as exc:
        # A rendering failure costs the convenience command, never the code.
        logger.error("Install command rendering failed (%s)", type(exc).__name__)
        minted.pop("install_command", None)
    minted["robot_bootstrap_sha256"] = script_sha256
    return Response(
        content=json.dumps(minted, separators=(",", ":")).encode("utf-8"),
        status_code=201,
        media_type="application/json",
    )


@router.get("/api/admin/robots/bootstrap")
def robot_bootstrap_info(
    user=Depends(auth.require_role("admin")),
) -> Dict[str, Optional[str]]:
    """The agent version this vault serves and the command that upgrades a robot to it."""
    script = robot_bootstrap.read_script()
    script_sha256 = robot_bootstrap.script_sha256(script)
    vault = robot_bootstrap.vault_url()
    return {
        "agent_version": robot_bootstrap.agent_version(script) if script is not None else None,
        "robot_bootstrap_sha256": script_sha256,
        "upgrade_command": (
            robot_bootstrap.upgrade_command(vault=vault, script_sha256=script_sha256)
            if script_sha256 and vault
            else None
        ),
    }


@router.delete("/api/admin/robots/{robot_id}/enrollment-code/{code_id}")
async def revoke_enrollment_code(
    robot_id: str,
    code_id: str,
    user=Depends(auth.require_role("admin")),
) -> Response:
    return await _proxy(
        method="DELETE",
        path=_upstream_path(robot_id, "enrollment-codes", code_id),
        user=user,
    )


async def _state_action_payload(request: Request, action: str) -> bytes:
    try:
        supplied = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    if not isinstance(supplied, dict) or set(supplied) - {"reason"}:
        raise HTTPException(status_code=422, detail="invalid_state_action")
    reason = supplied.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=422, detail="invalid_state_action")

    body: Dict[str, str] = {"state": _ACTION_STATES[action]}
    if reason:
        body["reason"] = reason
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


@router.post("/api/admin/robots/{robot_id}/{action}")
async def change_robot_state(
    robot_id: str,
    action: str,
    request: Request,
    user=Depends(auth.require_role("admin")),
) -> Response:
    if action not in _ACTION_STATES:
        raise HTTPException(status_code=404, detail="Not Found")
    return await _proxy(
        method="POST",
        path=_upstream_path(robot_id, "state"),
        user=user,
        payload=await _state_action_payload(request, action),
    )


@router.post("/api/admin/robots/{robot_id}/credentials/{credential_id}/revoke")
async def revoke_credential(
    robot_id: str,
    credential_id: str,
    request: Request,
    user=Depends(auth.require_role("admin")),
) -> Response:
    return await _proxy(
        method="POST",
        path=_upstream_path(robot_id, "credentials", credential_id, "revoke"),
        user=user,
        payload=await _request_body(request),
    )
