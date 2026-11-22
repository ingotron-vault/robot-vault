"""`GET /robot` and `GET /robot/agent-linux-amd64.tar.gz` (query side).

Ways this could go wrong, each covered below:
- the routes demand a token (a robot has none) or hide behind the viewer guard;
- the served script differs from the bytes the install-command digest covers;
- a missing or wrong file leaks a path or an OS error, or serves something else;
- a file larger than the bound is streamed from an unauthenticated route.
"""
from __future__ import annotations

import hashlib
import importlib

import pytest
from fastapi.testclient import TestClient

from query_service import auth, robot_bootstrap, settings

SCRIPT = b"#!/bin/sh\nAGENT_VERSION='0.11.0'\nexit 0\n"
TARBALL = b"\x1f\x8b" + bytes(range(256)) * 64


def _import_query_service_main():
    import fastapi.staticfiles as fastapi_staticfiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    original = fastapi_staticfiles.StaticFiles
    fastapi_staticfiles.StaticFiles = _StubStaticFiles
    try:
        return importlib.import_module("query_service.main")
    finally:
        fastapi_staticfiles.StaticFiles = original


query_main = _import_query_service_main()
client = TestClient(query_main.app, raise_server_exceptions=False)


@pytest.fixture
def bootstrap_dir(tmp_path, monkeypatch):
    (tmp_path / "robot.sh").write_bytes(SCRIPT)
    (tmp_path / "agent-linux-amd64.tar.gz").write_bytes(TARBALL)
    monkeypatch.setattr(settings, "ROBOT_BOOTSTRAP_DIR", str(tmp_path))
    # Real OIDC mode with no usable token: any guard on these routes would 401.
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", True)
    return tmp_path


def test_script_is_public_and_byte_identical_to_the_digest_source(bootstrap_dir):
    response = client.get("/robot")

    assert response.status_code == 200
    assert response.content == SCRIPT
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert hashlib.sha256(response.content).hexdigest() == robot_bootstrap.script_sha256(
        robot_bootstrap.read_script()
    )
    # The same process does guard data routes, so "public" above is real.
    assert client.get("/api/v1/robots/list").status_code == 401


def test_agent_tarball_is_public_and_verbatim(bootstrap_dir):
    response = client.get("/robot/agent-linux-amd64.tar.gz")

    assert response.status_code == 200
    assert response.content == TARBALL
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/robot", "/robot/agent-linux-amd64.tar.gz"])
@pytest.mark.parametrize("how", ["missing", "unset", "directory"])
def test_missing_material_is_a_bounded_503_without_detail(bootstrap_dir, monkeypatch, path, how):
    if how == "unset":
        monkeypatch.setattr(settings, "ROBOT_BOOTSTRAP_DIR", "")
    elif how == "missing":
        for name in ("robot.sh", "agent-linux-amd64.tar.gz"):
            (bootstrap_dir / name).unlink()
    else:
        for name in ("robot.sh", "agent-linux-amd64.tar.gz"):
            (bootstrap_dir / name).unlink()
            (bootstrap_dir / name).mkdir()

    response = client.get(path)

    assert response.status_code == 503
    assert response.json() == {"error": "robot_bootstrap_unavailable"}
    assert str(bootstrap_dir) not in response.text


def test_oversized_script_is_refused(bootstrap_dir):
    (bootstrap_dir / "robot.sh").write_bytes(b"#" * (robot_bootstrap.SCRIPT_MAX_BYTES + 1))
    assert client.get("/robot").status_code == 503
    assert robot_bootstrap.read_script() is None


def test_other_names_under_robot_are_not_served(bootstrap_dir):
    (bootstrap_dir / "secret.txt").write_bytes(b"not public")
    for path in ("/robot/secret.txt", "/robot/../robot.sh", "/robot/robot.sh"):
        assert client.get(path).status_code == 404, path


def test_default_directory_is_the_image_location():
    assert settings.ROBOT_BOOTSTRAP_DIR == "/opt/robot-vault/robot"
