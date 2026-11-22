"""Unit tests for the core/pro edition split.

The edition gate has two halves and this file pins both:

  1. `settings.read_edition()` -- what `ROBOT_VAULT_EDITION` is allowed to say.
     Unset and empty mean "core" (the default; an empty value is a `.env` line
     with nothing after the `=`, i.e. an omission). A typo raises rather than
     defaulting in either direction -- see the rationale in `settings.py`.

  2. `main.create_app()` -- what the app is SHAPED like once the edition is
     resolved. In core, the Pro package is never imported,
     so its paths 404 *and* are absent from `/openapi.json`; the core surface
     (uploads, robots, the lake, admin, `/healthz`) is untouched, and
     `/api/v1/edition` answers. These tests pass with or without the Pro
     package installed.

Building the app twice is deliberate and is why `main.py` has a factory.
The alternative -- `importlib.reload(query_service.main)` -- would
mutate the module object every other test file in this directory already
holds (`qsm.app`), so a core-edition reload would silently delete the pro
routes from THEIR client too, in whatever order pytest happened to collect
them. A second app object touches nothing global: `middleware.install()` only
attaches middleware to the app it is handed, and the Prometheus metrics it
uses are module-level singletons created once at import (they must be -- the
default registry rejects a duplicate registration).

`StaticFiles` is stubbed per call for the same reason the sibling test files
stub it at import time: `main.py` mounts the SPA build output, which only
exists inside the query service's Docker image.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys

import pytest


def _import_query_service_main():
    """See test_bucket_validation.py / test_query_sql_auth.py: main.py mounts
    StaticFiles over the SPA build output, which only exists in the image."""
    if "query_service.main" in sys.modules:
        return sys.modules["query_service.main"]

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


qsm = _import_query_service_main()

from fastapi.testclient import TestClient  # noqa: E402

from query_service import settings  # noqa: E402


class _StubStaticFiles:
    def __init__(self, *args, **kwargs):
        pass


def _client_for(monkeypatch, edition: str) -> TestClient:
    """Build a fresh app for `edition` and return a client against it.

    Goes through the real env reader -- `monkeypatch.setenv` then
    `settings.read_edition()` -- rather than assigning the literal, so a
    regression in the parsing half shows up here too.
    """
    monkeypatch.setenv("ROBOT_VAULT_EDITION", edition)
    monkeypatch.setattr(settings, "EDITION", settings.read_edition())
    # main.py binds `StaticFiles` in its own namespace at import time, so the
    # patch has to land on the module that will call it.
    monkeypatch.setattr(qsm, "StaticFiles", _StubStaticFiles)
    return TestClient(qsm.create_app(), raise_server_exceptions=False)


# --- settings.read_edition() -------------------------------------------------


def test_edition_defaults_to_core_when_unset():
    assert settings.read_edition({}) == "core"
    assert settings.DEFAULT_EDITION == "core"


def test_empty_value_means_the_default_not_an_error():
    """`ROBOT_VAULT_EDITION=` in a .env is an omission, not a choice."""
    assert settings.read_edition({"ROBOT_VAULT_EDITION": ""}) == "core"
    assert settings.read_edition({"ROBOT_VAULT_EDITION": "   "}) == "core"


@pytest.mark.parametrize("raw", ["core", "CORE", " Core ", "pro", "PRO", "\tpro\n"])
def test_known_editions_are_case_and_whitespace_insensitive(raw):
    assert settings.read_edition({"ROBOT_VAULT_EDITION": raw}) in settings.EDITIONS
    assert settings.read_edition({"ROBOT_VAULT_EDITION": raw}) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["cor", "enterprise", "core,pro", "true", "1"])
def test_unknown_edition_raises_rather_than_guessing(raw):
    """Defaulting a typo to "pro" would ship pro surfaces to a core box;
    defaulting it to "core" would silently delete a pro customer's features.
    Neither is safe, so this refuses at import."""
    with pytest.raises(ValueError) as excinfo:
        settings.read_edition({"ROBOT_VAULT_EDITION": raw})
    assert raw in str(excinfo.value)


# --- core: the pro surface does not exist ------------------------------------

PRO_PREFIXES = ("/api/v1/episodes", "/api/v1/datasets")


def test_core_404s_the_pro_routes(monkeypatch):
    client = _client_for(monkeypatch, "core")
    assert client.get("/api/v1/episodes").status_code == 404
    assert client.get("/api/v1/datasets").status_code == 404
    assert client.get("/api/v1/episodes/ep-1/detail").status_code == 404


def test_core_openapi_does_not_advertise_the_pro_routes(monkeypatch):
    """404 alone is not the deliverable: a core box must not DOCUMENT an API
    it does not serve. Anything a customer can read out of /openapi.json is a
    promise."""
    client = _client_for(monkeypatch, "core")
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith(PRO_PREFIXES)]
    assert "episodes" not in client.get("/openapi.json").text.lower()


def test_core_keeps_the_core_surface(monkeypatch):
    """The split removes the Pro features, not the platform: upload -> integrity ->
    Parquet -> query/UI is exactly what `core` is."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = _client_for(monkeypatch, "core")
    paths = client.get("/openapi.json").json()["paths"]
    for kept in ("/api/v1/uploads/list", "/api/robots", "/healthz", "/api/selfcheck"):
        assert kept in paths, kept
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/v1/uploads/list").status_code == 200


def test_core_edition_endpoint_reports_core(monkeypatch):
    from robot_vault_config.edition import ADAPTER_API, CORE_VERSION

    client = _client_for(monkeypatch, "core")
    resp = client.get("/api/v1/edition")
    assert resp.status_code == 200
    assert resp.json() == {
        "edition": "core",
        "core_version": CORE_VERSION,
        "adapter_api": ADAPTER_API,
        "pro_version": None,
        "capabilities": [],
        # This service's own MCP config. AUTH_MODE=none here, so MCP is off.
        "mcp": {"enabled": False, "write_tools": False},
    }


def test_core_readyz_keeps_the_dataset_signing_key(monkeypatch):
    """Pro adds a dataset-signing readiness check. Core reports
    `checks.dataset_signing` as true so `/readyz` has the same shape in both
    editions."""
    client = _client_for(monkeypatch, "core")
    monkeypatch.setattr(qsm, "_postgres_is_ready", lambda: True)
    body = client.get("/readyz").json()
    assert body["checks"]["dataset_signing"] is True


def test_core_app_process_never_imports_pro():
    """A fresh core process builds the module-level app without loading any
    Pro module, whether or not the Pro package is installed."""
    script = (
        "import json, sys\n"
        "import fastapi.staticfiles as s\n"
        "s.StaticFiles = type('Stub', (), {'__init__': lambda self, *a, **k: None})\n"
        "import query_service.main as m\n"
        "print(json.dumps({'edition': m.app.state.identity['edition'],"
        " 'pro': sorted(n for n in sys.modules if n.split('.')[0] == 'robot_vault_pro')}))\n"
    )
    env = {**__import__("os").environ, "ROBOT_VAULT_EDITION": "core"}
    out = subprocess.run(
        [sys.executable, "-c", script], env=env, check=True, capture_output=True, text=True
    ).stdout
    assert json.loads(out.strip().splitlines()[-1]) == {"edition": "core", "pro": []}


# --- both editions ------------------------------------------------------------


def test_edition_endpoint_is_open(monkeypatch):
    """Unauthenticated even under AUTH_MODE=oidc, like /healthz: the SPA has
    to be able to ask before it can decide whether to fetch a token."""
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", True)
    client = _client_for(monkeypatch, "core")
    assert client.get("/api/v1/edition").status_code == 200
