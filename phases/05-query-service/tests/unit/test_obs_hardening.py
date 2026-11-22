"""Security and resource bounds for the allowlisted Prometheus API."""

import asyncio

import pytest
from fastapi import HTTPException

from query_service.routers import obs


@pytest.mark.parametrize(
    "step",
    ["", "0s", "1 second", "30s;drop", f"{'9' * 5_000}s"],
)
def test_promql_step_allowlist_rejects_invalid_values(step):
    with pytest.raises(HTTPException) as exc_info:
        obs._step_seconds(step)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_promql_rejects_non_finite_ranges_before_network(value):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(obs.promql_range("up", start=value, end=100.0, step="30s"))
    assert exc_info.value.detail == "invalid_range"


def test_promql_rejects_excessive_point_counts_before_network():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(obs.promql_range("up", start=0.0, end=86_400.0, step="1ms"))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "too_many_points"


def test_promql_preserves_an_explicit_zero_timestamp(monkeypatch):
    async def capture(path, *, params=None, timeout):
        return {"path": path, "params": params, "timeout": timeout}

    monkeypatch.setattr(obs, "_bounded_prometheus_json", capture)
    result = asyncio.run(obs.promql_range("up", start=0.0, end=60.0, step="30s"))

    assert result["params"]["start"] == "0.0"
    assert result["params"]["end"] == "60.0"


def test_prometheus_response_is_bounded_before_json_decode(monkeypatch):
    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            yield b"abc"
            yield b"de"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(obs, "_PROMETHEUS_RESPONSE_MAX_BYTES", 4)
    monkeypatch.setattr(obs.httpx, "AsyncClient", lambda **kwargs: Client())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(obs._bounded_prometheus_json("api/v1/alerts", timeout=1.0))
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "prometheus_response_too_large"


def test_prometheus_reads_decoded_compressed_response_bytes(monkeypatch):
    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            # HTTPX has already decoded a gzip upstream at this boundary.
            yield b'{"status":"success","data":'
            yield b'{"result":[]}}'

        async def aiter_raw(self):
            raise AssertionError("compressed wire bytes must not be parsed as JSON")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(obs.httpx, "AsyncClient", lambda **kwargs: Client())

    result = asyncio.run(
        obs._bounded_prometheus_json("api/v1/alerts", timeout=1.0)
    )
    assert result == {"status": "success", "data": {"result": []}}


def test_obs_health_reads_only_upstream_status_not_body(monkeypatch):
    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            raise AssertionError("health response body must not be consumed")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return Response()

        async def get(self, *args, **kwargs):
            raise AssertionError("buffering client.get must not be used")

    monkeypatch.setattr(obs.httpx, "AsyncClient", lambda **kwargs: Client())

    result = asyncio.run(obs.obs_health())
    assert result == {
        "status": {"prometheus": {"ok": True, "status": 200}},
    }


def test_generic_prometheus_proxy_is_not_exposed():
    paths = {route.path for route in obs.router.routes}
    assert "/obs/prometheus/{path:path}" not in paths
    assert paths >= {
        "/api/obs/promql/range",
        "/api/obs/alerts",
        "/api/obs/health",
    }
