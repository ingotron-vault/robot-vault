"""Hermetic contract tests for the query-to-ingest provisioning proxy."""
from __future__ import annotations

import hashlib
import json
import importlib
import shlex
import sys
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from query_service import auth, robot_bootstrap, settings
from query_service.routers import robots_admin


TOKEN = "p" * 48
VAULT = "https://vault.example"
SCRIPT = b"#!/bin/sh\nAGENT_VERSION='1.2.3'\necho robot\n"
SCRIPT_SHA256 = hashlib.sha256(SCRIPT).hexdigest()
CODE = "ign1." + "A" * 26 + "." + "B" * 32


def _expected_command(args: str) -> str:
    return (
        f"f=$(mktemp) && curl -fsSk --proto =https {VAULT}/robot -o \"$f\" \\\n"
        f"  && echo \"{SCRIPT_SHA256}  $f\" | sha256sum -c - \\\n"
        f"  && sudo sh \"$f\" {args}"
    )


class _BufferedResponse:
    """Make an already-materialized httpx test response look streamed."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.headers = response.headers
        self._content = response.content
        self.is_closed = False

    async def aiter_raw(self):
        if self._content:
            yield self._content

    async def aclose(self) -> None:
        self.is_closed = True


class StubUpstream:
    """A tiny async HTTPX stand-in that records only proxy-to-ingest traffic."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.response: httpx.Response | BaseException = httpx.Response(200, json={})
        self.client_kwargs: Dict[str, Any] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def build_request(self, method, url, *, headers, content):
        return {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "content": content,
        }

    async def send(self, request, *, stream):
        self.calls.append(
            {
                "method": request["method"],
                "url": request["url"],
                "headers": request["headers"],
                "content": request["content"],
                "stream": stream,
            }
        )
        if isinstance(self.response, BaseException):
            raise self.response
        # ``httpx.Response(json=...)`` has already consumed its in-memory
        # stream. Production ``send(..., stream=True)`` has not, so adapt only
        # this convenient fixture shape while retaining a true streaming path
        # for the chunk-cap regression below.
        if isinstance(self.response, httpx.Response) and self.response.is_stream_consumed:
            self.response = _BufferedResponse(self.response)
        return self.response


@pytest.fixture
def upstream(monkeypatch) -> StubUpstream:
    stub = StubUpstream()

    def client_factory(**kwargs):
        stub.client_kwargs = kwargs
        return stub

    monkeypatch.setattr(robots_admin.httpx, "AsyncClient", client_factory)
    return stub


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "RELEASE_MODE", False)
    monkeypatch.setattr(settings, "INGEST_PROVISIONING_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "INGEST_PROVISIONING_UPSTREAM", "http://ingest:8080")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", VAULT)
    (tmp_path / "robot.sh").write_bytes(SCRIPT)
    monkeypatch.setattr(settings, "ROBOT_BOOTSTRAP_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "get_current_user_optional", lambda _request: auth.User("admin-7", ["admin"]))
    app = FastAPI()
    app.include_router(robots_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def _json_body(call: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(call["content"])


def test_create_proxies_only_the_provisioning_token_and_preserves_success(client, upstream):
    upstream.response = httpx.Response(
        201,
        json={"robot_id": "rover-7", "state": "pending", "auth_mode": "per_robot"},
    )

    response = client.post(
        "/api/admin/robots",
        json={"robot_id": "rover-7", "name": "Rover 7"},
        headers={"Authorization": "Bearer browser-jwt", "Cookie": "session=browser-only"},
    )

    assert response.status_code == 201
    assert response.json()["robot_id"] == "rover-7"
    assert len(upstream.calls) == 1
    call = upstream.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://ingest:8080/admin/v2/robots"
    assert _json_body(call) == {"robot_id": "rover-7", "name": "Rover 7"}
    assert call["headers"] == {
        "accept": "application/json",
        "authorization": f"Bearer {TOKEN}",
        "x-ingotron-actor": "admin-7",
        "content-type": "application/json",
    }
    assert upstream.client_kwargs["follow_redirects"] is False
    assert call["stream"] is True


def test_lifecycle_list_preserves_ingest_json_and_status(client, upstream):
    upstream.response = httpx.Response(
        200,
        json={"robots": [{"robot_id": "rover-7", "credentials": [], "open_codes": 0}]},
    )

    response = client.get("/api/admin/robots/lifecycle")

    assert response.status_code == 200
    assert response.json()["robots"][0]["robot_id"] == "rover-7"
    assert upstream.calls == [
        {
            "method": "GET",
            "url": "http://ingest:8080/admin/v2/robots",
            "headers": {
                "accept": "application/json",
                "authorization": f"Bearer {TOKEN}",
                "x-ingotron-actor": "admin-7",
            },
            "content": None,
            "stream": True,
        }
    ]


def test_mint_adds_the_server_rendered_install_command(client, upstream):
    code = "ign1." + "A" * 26 + "." + "B" * 32
    upstream.response = httpx.Response(
        201,
        json={
            "code": code,
            "code_id": "code-1",
            "fingerprint": "0123456789ab",
            "expires_at": "2026-08-20T01:00:00Z",
            "ca_spki_sha256": "a" * 64,
            "vault_ingest_endpoint": "vault.example:50051",
        },
    )

    response = client.post("/api/admin/robots/rover-7/enrollment-code", json={"purpose": "enroll"})

    assert response.status_code == 201
    body = response.json()
    assert body["install_command"] == _expected_command(
        f"--vault {VAULT} --ingest vault.example:50051 --code {code}"
    )
    assert body["robot_bootstrap_sha256"] == SCRIPT_SHA256
    assert body["vault_ingest_endpoint"] == "vault.example:50051"
    assert body["install_mode"] == "install"
    assert upstream.calls[0]["url"].endswith("/rover-7/enrollment-codes")
    assert _json_body(upstream.calls[0]) == {"purpose": "enroll"}


def test_mint_without_public_base_url_returns_the_code(client, upstream, monkeypatch):
    """Ingest has already spent work minting a one-use code: never drop it."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    code = "ign1." + "A" * 26 + "." + "B" * 32
    upstream.response = httpx.Response(201, json={"code": code, "code_id": "code-1"})

    response = client.post("/api/admin/robots/rover-7/enrollment-code", json={"purpose": "enroll"})

    assert response.status_code == 201
    assert response.json()["code"] == code
    assert len(upstream.calls) == 1


def test_reenrollment_mint_renders_the_reenroll_command(client, upstream):
    code = "ign1." + "A" * 26 + "." + "B" * 32
    upstream.response = httpx.Response(201, json={"code": code, "vault_ingest_endpoint": "ingest.example:7443"})

    response = client.post("/api/admin/robots/rover-7/enrollment-code", json={"purpose": "reenroll"})

    assert response.status_code == 201
    assert response.json()["install_mode"] == "reenroll"
    assert response.json()["install_command"] == _expected_command(
        f"--reenroll --vault {VAULT} --ingest ingest.example:7443 --code {code}"
    )
    assert _json_body(upstream.calls[0]) == {"purpose": "reenroll"}


def test_mint_renders_a_validated_recording_root_without_forwarding_it_to_ingest(client, upstream):
    code = "ign1." + "A" * 26 + "." + "B" * 32
    upstream.response = httpx.Response(201, json={"code": code, "vault_ingest_endpoint": "ingest.example:50051"})

    response = client.post(
        "/api/admin/robots/rover-7/enrollment-code",
        json={"purpose": "enroll", "recording_root": "/mnt/robot_recordings"},
    )

    assert response.status_code == 201
    assert response.json()["recording_root"] == "/mnt/robot_recordings"
    assert response.json()["install_command"].endswith("--root /mnt/robot_recordings")
    assert _json_body(upstream.calls[0]) == {"purpose": "enroll"}


def _break_script(monkeypatch, tmp_path, how: str) -> None:
    if how == "missing":
        (tmp_path / "robot.sh").unlink()
    elif how == "unreadable":
        (tmp_path / "robot.sh").unlink()
        (tmp_path / "robot.sh").mkdir()  # open() fails with IsADirectoryError
    elif how == "no_public_url":
        monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    elif how == "http_public_url":
        monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "http://vault.example")
    elif how == "rendering_fails":
        def boom(**_kwargs):
            raise RuntimeError("render failed")

        monkeypatch.setattr(robot_bootstrap, "install_command", boom)


@pytest.mark.parametrize("how", ["missing", "unreadable", "no_public_url", "http_public_url", "rendering_fails"])
@pytest.mark.parametrize("purpose, mode", [("enroll", "install"), ("reenroll", "reenroll")])
def test_mint_keeps_code_and_recovery_metadata_when_no_command_can_be_rendered(
    client, upstream, monkeypatch, tmp_path, how, purpose, mode
):
    """The code, mode and root survive every command failure; one mint only."""
    _break_script(monkeypatch, tmp_path, how)
    upstream.response = httpx.Response(
        201, json={"code": CODE, "code_id": "c1", "vault_ingest_endpoint": "ingest.example:50051"}
    )

    response = client.post(
        "/api/admin/robots/rover-7/enrollment-code",
        json={"purpose": purpose, "recording_root": "/srv/recordings"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["code"] == CODE
    assert body["install_mode"] == mode
    assert body["recording_root"] == "/srv/recordings"
    assert body["vault_ingest_endpoint"] == "ingest.example:50051"
    assert "install_command" not in body
    if how in ("missing", "unreadable"):
        # Fail closed: no digest means nothing the robot could check.
        assert body["robot_bootstrap_sha256"] is None
    else:
        assert body["robot_bootstrap_sha256"] == SCRIPT_SHA256
    assert len(upstream.calls) == 1


def test_install_command_is_byte_identical_to_the_ui_fallback(client, upstream, monkeypatch, tmp_path):
    """Pinned against ui-spa/src/lib/enrollment.ts renderBootstrapCommand + shellQuote."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://vault.example:8443")
    upstream.response = httpx.Response(201, json={"code": CODE, "vault_ingest_endpoint": "[fd00::7]:50051"})

    response = client.post(
        "/api/admin/robots/rover-7/enrollment-code",
        json={"purpose": "reenroll", "recording_root": "/srv/robot~data"},
    )

    assert response.json()["install_command"] == (
        "f=$(mktemp) && curl -fsSk --proto =https https://vault.example:8443/robot -o \"$f\" \\\n"
        f"  && echo \"{SCRIPT_SHA256}  $f\" | sha256sum -c - \\\n"
        "  && sudo sh \"$f\" --reenroll --vault https://vault.example:8443 --ingest '[fd00::7]:50051'"
        f" --code {CODE} --root '/srv/robot~data'"
    )


def test_shell_quote_matches_the_ui_rule():
    assert robot_bootstrap.shell_quote("ingest.example:50051") == "ingest.example:50051"
    assert robot_bootstrap.shell_quote("") == "''"
    assert robot_bootstrap.shell_quote("a b") == "'a b'"
    assert robot_bootstrap.shell_quote("it's") == "'it'\\''s'"


def test_every_substituted_argument_is_shell_quoted(client, upstream):
    """Hostile code text stays one argv word."""
    code = "ign1.x'; touch /tmp/pwned; echo '"
    upstream.response = httpx.Response(201, json={"code": code, "vault_ingest_endpoint": "ingest.example:50051"})

    response = client.post(
        "/api/admin/robots/rover-7/enrollment-code",
        json={"purpose": "enroll", "recording_root": "/srv/robot-data"},
    )

    command = response.json()["install_command"]
    run_part = command.split("&& sudo sh \"$f\" ", 1)[1]
    assert shlex.split(run_part) == [
        "--vault", VAULT, "--ingest", "ingest.example:50051", "--code", code, "--root", "/srv/robot-data",
    ]


@pytest.mark.parametrize("endpoint", [None, 'ingest "host":5 $(id)', "ingest.example", "ingest.example:0", 7])
def test_no_valid_ingest_endpoint_means_no_command_but_the_code(client, upstream, endpoint):
    upstream.response = httpx.Response(201, json={"code": CODE, "vault_ingest_endpoint": endpoint})

    response = client.post("/api/admin/robots/rover-7/enrollment-code", json={"purpose": "enroll"})

    assert response.status_code == 201
    assert response.json()["code"] == CODE
    assert "install_command" not in response.json()
    assert response.json()["robot_bootstrap_sha256"] == SCRIPT_SHA256


def test_bootstrap_info_gives_version_digest_and_upgrade_command(client, upstream):
    response = client.get("/api/admin/robots/bootstrap")

    assert response.status_code == 200
    assert response.json() == {
        "agent_version": "1.2.3",
        "robot_bootstrap_sha256": SCRIPT_SHA256,
        "upgrade_command": _expected_command(f"--upgrade --vault {VAULT}"),
    }
    assert upstream.calls == []


def test_bootstrap_info_without_script_or_public_url_has_no_command(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    assert client.get("/api/admin/robots/bootstrap").json()["upgrade_command"] is None

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", VAULT)
    (tmp_path / "robot.sh").unlink()
    assert client.get("/api/admin/robots/bootstrap").json() == {
        "agent_version": None,
        "robot_bootstrap_sha256": None,
        "upgrade_command": None,
    }


@pytest.mark.parametrize("user, status", [(None, 401), (auth.User("operator", ["operator"]), 403)])
def test_bootstrap_info_is_admin_only(client, monkeypatch, user, status):
    monkeypatch.setattr(auth, "get_current_user_optional", lambda _request: user)
    assert client.get("/api/admin/robots/bootstrap").status_code == status


@pytest.mark.parametrize(
    "recording_root",
    [
        "relative/path",
        "/etc/recordings",
        "/data/../etc",
        "/data/$HOME",
        "/srv/recordings /etc",
        "/srv/recordings\t/etc",
        " /srv/recordings",
        "/srv/recordings ",
    ],
)
def test_mint_rejects_unsafe_recording_roots_before_contacting_ingest(client, upstream, recording_root):
    response = client.post(
        "/api/admin/robots/rover-7/enrollment-code",
        json={"purpose": "enroll", "recording_root": recording_root},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_recording_root"}
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("action", "upstream_state"),
    [("disable", "disabled"), ("enable", "enrolled"), ("decommission", "decommissioned")],
)
def test_lifecycle_actions_translate_only_the_route_action(client, upstream, action, upstream_state):
    upstream.response = httpx.Response(200, json={"state": upstream_state})

    response = client.post(f"/api/admin/robots/rover-7/{action}", json={"reason": "operator request"})

    assert response.status_code == 200
    assert upstream.calls[0]["url"].endswith("/rover-7/state")
    assert _json_body(upstream.calls[0]) == {"state": upstream_state, "reason": "operator request"}


def test_revoke_routes_are_exact_and_keep_the_upstream_status(client, upstream):
    upstream.response = httpx.Response(204)
    code_response = client.delete("/api/admin/robots/rover-7/enrollment-code/code-7")
    assert code_response.status_code == 204
    assert upstream.calls[-1]["url"].endswith("/rover-7/enrollment-codes/code-7")

    upstream.response = httpx.Response(200, json={"credential_id": "cred-7", "revoked": True})
    credential_response = client.post(
        "/api/admin/robots/rover-7/credentials/cred-7/revoke",
        json={"reason": "lost hardware"},
    )
    assert credential_response.status_code == 200
    assert credential_response.json()["revoked"] is True
    assert upstream.calls[-1]["url"].endswith("/rover-7/credentials/cred-7/revoke")
    assert _json_body(upstream.calls[-1]) == {"reason": "lost hardware"}


def test_ingest_client_refusal_is_preserved_but_ingest_failure_is_not(client, upstream):
    upstream.response = httpx.Response(409, json={"error": "robot_id_exists"})
    response = client.post("/api/admin/robots", json={"robot_id": "rover-7"})
    assert response.status_code == 409
    assert response.json() == {"error": "robot_id_exists"}

    upstream.response = httpx.Response(503, json={"error": "secret-internal-detail"})
    response = client.post("/api/admin/robots", json={"robot_id": "rover-8"})
    assert response.status_code == 502
    assert response.json() == {"error": "provisioning_unavailable"}
    assert response.headers["content-type"] == "application/json"
    assert "secret-internal-detail" not in response.text


@pytest.mark.parametrize("status", [301, 401, 403, 500, 503])
def test_internal_redirect_auth_and_server_failures_are_sanitized(client, upstream, status):
    upstream.response = httpx.Response(
        status,
        json={"error": "private-upstream-detail"},
        headers={"location": "http://unexpected.internal/"},
    )

    response = client.get("/api/admin/robots/lifecycle")

    assert response.status_code == 502
    assert response.json() == {"error": "provisioning_unavailable"}
    assert "private-upstream-detail" not in response.text
    assert "unexpected.internal" not in response.headers.get("location", "")


@pytest.mark.parametrize("body", [[], "not-an-object", None])
def test_malformed_successful_mint_is_not_rendered_or_executed(client, upstream, body):
    upstream.response = httpx.Response(201, json=body)

    response = client.post(
        "/api/admin/robots/rover-7/enrollment-code",
        json={"purpose": "enroll"},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "provisioning_unavailable"}


@pytest.mark.parametrize("user, status", [(None, 401), (auth.User("operator", ["operator"]), 403)])
def test_only_admins_reach_the_provisioning_upstream(client, upstream, monkeypatch, user, status):
    monkeypatch.setattr(auth, "get_current_user_optional", lambda _request: user)

    response = client.get("/api/admin/robots/lifecycle")

    assert response.status_code == status
    assert upstream.calls == []


def test_missing_or_untrusted_proxy_configuration_fails_closed(client, upstream, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_PROVISIONING_TOKEN", None)
    response = client.get("/api/admin/robots/lifecycle")
    assert response.status_code == 502
    assert response.json() == {"error": "provisioning_unavailable"}
    assert upstream.calls == []

    monkeypatch.setattr(settings, "INGEST_PROVISIONING_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "INGEST_PROVISIONING_UPSTREAM", "http://outside.example:8080")
    response = client.get("/api/admin/robots/lifecycle")
    assert response.status_code == 502
    assert upstream.calls == []


def test_transport_failure_is_sanitized(client, upstream):
    upstream.response = httpx.ConnectError("private connection detail")

    response = client.get("/api/admin/robots/lifecycle")

    assert response.status_code == 502
    assert response.json() == {"error": "provisioning_unavailable"}
    assert "private connection detail" not in response.text


class _FailIfReadStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read = False

    async def __aiter__(self):
        self.read = True
        raise AssertionError("declared oversize response must not be read")
        yield b""  # pragma: no cover - marks this as an async generator

    async def aclose(self) -> None:
        return None


def test_declared_oversize_upstream_reply_is_rejected_before_body_read(client, upstream):
    body = _FailIfReadStream()
    upstream.response = httpx.Response(
        200,
        headers={"content-length": str(robots_admin._MAX_UPSTREAM_RESPONSE_BYTES + 1)},
        stream=body,
    )

    response = client.get("/api/admin/robots/lifecycle")

    assert response.status_code == 502
    assert response.json() == {"error": "provisioning_unavailable"}
    assert body.read is False
    assert upstream.response.is_closed


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: List[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def test_chunked_oversize_upstream_reply_is_rejected_and_closed(client, upstream, monkeypatch):
    # Lowering the production cap is intentional: it verifies accounting across
    # chunks without allocating a 16 MiB test body.
    monkeypatch.setattr(robots_admin, "_MAX_UPSTREAM_RESPONSE_BYTES", 3)
    upstream.response = httpx.Response(200, stream=_ChunkedStream([b"abc", b"d"]))

    response = client.get("/api/admin/robots/lifecycle")

    assert response.status_code == 502
    assert response.json() == {"error": "provisioning_unavailable"}
    assert upstream.response.is_closed


def test_production_create_app_registers_all_robot_proxy_routes(monkeypatch):
    """Exercise the real factory, rather than a direct test-only router mount."""
    import fastapi.staticfiles as fastapi_staticfiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    # Unit test images do not need a built SPA.  Apply the stub before the
    # import as main.py creates its production module-level app at import time.
    monkeypatch.setattr(fastapi_staticfiles, "StaticFiles", _StubStaticFiles)
    query_main = sys.modules.get("query_service.main")
    if query_main is None:
        query_main = importlib.import_module("query_service.main")
    monkeypatch.setattr(query_main, "StaticFiles", _StubStaticFiles)
    monkeypatch.setattr(query_main.settings, "validate_auth_configuration", lambda: None)
    monkeypatch.setattr(query_main.settings, "EDITION", "core")

    # This FastAPI version retains included routers lazily instead of
    # flattening them into ``app.routes``. OpenAPI is the public, evaluated
    # route surface and cannot pass if create_app forgets the include.
    paths = TestClient(query_main.create_app()).get("/openapi.json").json()["paths"]
    robot_routes = {
        path: set(item)
        for path, item in paths.items()
        if path.startswith("/api/admin/robots")
    }

    assert robot_routes == {
        "/api/admin/robots": {"post"},
        "/api/admin/robots/lifecycle": {"get"},
        "/api/admin/robots/bootstrap": {"get"},
        "/api/admin/robots/{robot_id}/enrollment-code": {"post"},
        "/api/admin/robots/{robot_id}/enrollment-code/{code_id}": {"delete"},
        "/api/admin/robots/{robot_id}/{action}": {"post"},
        "/api/admin/robots/{robot_id}/credentials/{credential_id}/revoke": {"post"},
    }


def test_release_settings_require_a_strong_token_and_exact_internal_origin(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "RELEASE_MODE", True)
    monkeypatch.setattr(settings, "OIDC_DEV", False)
    monkeypatch.setattr(settings, "METRICS_AUTH_TOKEN", "m" * 32)
    monkeypatch.setattr(settings, "INGEST_PROVISIONING_TOKEN", None)
    with pytest.raises(ValueError, match="INGEST_PROVISIONING_TOKEN"):
        settings.validate_auth_configuration()

    monkeypatch.setattr(settings, "INGEST_PROVISIONING_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "INGEST_PROVISIONING_UPSTREAM", "https://ingest:8080")
    with pytest.raises(ValueError, match="INGEST_PROVISIONING_UPSTREAM"):
        settings.validate_auth_configuration()
