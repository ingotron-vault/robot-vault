"""Adversarial contracts for the query-service HTTP middleware boundary."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
import pytest

from query_service import middleware, settings


class _Redis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.counts: dict[str, int] = {}
        self.keys: list[str] = []
        self.expiries: dict[str, int] = {}
        self._pipeline_ops: list[tuple] = []

    def pipeline(self, *, transaction: bool):
        assert transaction is True
        self._pipeline_ops = []
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def incr(self, key: str):
        self.keys.append(key)
        if self.fail:
            raise ConnectionError("redis unavailable")
        self._pipeline_ops.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int, *, nx: bool):
        if self.fail:
            raise ConnectionError("redis unavailable")
        self._pipeline_ops.append(("expire", key, seconds, nx))
        return self

    def execute(self):
        results = []
        for operation in self._pipeline_ops:
            if operation[0] == "incr":
                key = operation[1]
                self.counts[key] = self.counts.get(key, 0) + 1
                results.append(self.counts[key])
            else:
                _, key, seconds, nx = operation
                changed = not nx or key not in self.expiries
                if changed:
                    self.expiries[key] = seconds
                results.append(changed)
        return results


@pytest.fixture(autouse=True)
def _reset_rate_state(monkeypatch):
    middleware._rate_counts.clear()
    middleware._redis_degradation_last_logged_at = 0.0
    monkeypatch.setattr(settings, "_redis", None)
    monkeypatch.setenv("RATE_LIMIT_DEFAULT_PER_MINUTE", "240")
    monkeypatch.setenv("RATE_LIMIT_ADMIN_PER_MINUTE", "60")
    monkeypatch.setenv("RATE_LIMIT_PRESIGN_PER_MINUTE", "120")


def _app() -> FastAPI:
    app = FastAPI()
    middleware.install(app)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"item_id": item_id}

    @app.get("/ui/")
    def ui_index():
        return PlainTextResponse("index")

    @app.get("/ui/assets/{asset}")
    def ui_asset(asset: str):
        return PlainTextResponse(asset)

    @app.post("/echo-size")
    async def echo_size(request: Request):
        return {"size": len(await request.body())}

    return app


def test_redis_rate_key_is_a_bounded_policy_not_the_raw_path(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(settings, "_redis", redis)
    monkeypatch.setenv("RATE_LIMIT_DEFAULT_PER_MINUTE", "1")

    client = TestClient(_app())
    assert client.get("/items/one").status_code == 200
    assert client.get("/items/two").status_code == 429

    assert len(set(redis.keys)) == 1
    assert ":default:" in redis.keys[0]
    assert "/items/" not in redis.keys[0]


def test_unavailable_redis_falls_back_to_memory_instead_of_opening_the_gate(monkeypatch):
    monkeypatch.setattr(settings, "_redis", _Redis(fail=True))
    monkeypatch.setenv("RATE_LIMIT_DEFAULT_PER_MINUTE", "1")

    client = TestClient(_app())
    assert client.get("/items/one").status_code == 200
    assert client.get("/items/two").status_code == 429


def test_redis_degradation_is_counted_and_logged_without_exception_details(
    monkeypatch,
    caplog,
):
    class Metric:
        count = 0

        def inc(self):
            self.count += 1

    class SecretFailureRedis(_Redis):
        def incr(self, key: str):
            raise ConnectionError("redis://operator:super-secret@redis.internal/0")

    metric = Metric()
    monkeypatch.setattr(middleware, "RATE_LIMIT_REDIS_DEGRADATIONS_TOTAL", metric)
    monkeypatch.setattr(settings, "_redis", SecretFailureRedis())
    caplog.set_level("WARNING", logger="query_service.middleware")

    client = TestClient(_app())
    assert client.get("/items/one").status_code == 200
    assert client.get("/items/two").status_code == 200

    assert metric.count == 2
    assert caplog.text.count("Redis rate limiter unavailable") == 1
    assert "ConnectionError" in caplog.text
    assert "super-secret" not in caplog.text


def test_redis_rate_counter_and_ttl_are_one_transaction():
    redis = _Redis()
    assert middleware._redis_increment(redis, "rl:test") == 1
    assert redis.expiries == {"rl:test": 60}
    assert middleware._redis_increment(redis, "rl:test") == 2
    assert redis.expiries == {"rl:test": 60}


def test_cors_has_an_explicit_dev_allowlist(monkeypatch):
    client = TestClient(_app())
    headers = {
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "GET",
    }
    allowed = client.options("/items/one", headers=headers)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

    headers["Origin"] = "https://attacker.example"
    refused = client.options("/items/one", headers=headers)
    assert "access-control-allow-origin" not in refused.headers


def test_hashed_assets_are_immutable_but_entry_html_is_not(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_HSTS", True)
    client = TestClient(_app(), base_url="https://vault.example")

    asset = client.get("/ui/assets/app-deadbeef.js")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.headers["content-security-policy"].endswith("frame-ancestors 'none'")
    assert "base-uri 'none'" in asset.headers["content-security-policy"]
    assert "object-src 'none'" in asset.headers["content-security-policy"]
    assert "form-action 'self'" in asset.headers["content-security-policy"]
    assert asset.headers["strict-transport-security"] == "max-age=31536000"

    entry = client.get("/ui/")
    assert entry.headers["cache-control"] == "no-store"
    assert entry.headers["pragma"] == "no-cache"


def test_declared_oversized_body_is_rejected_before_handler(monkeypatch):
    monkeypatch.setenv("QUERY_MAX_REQUEST_BODY_BYTES", "4")
    response = TestClient(_app()).post("/echo-size", content=b"abcde")
    assert response.status_code == 413


def test_exact_request_body_limit_is_accepted(monkeypatch):
    monkeypatch.setenv("QUERY_MAX_REQUEST_BODY_BYTES", "4")
    response = TestClient(_app()).post("/echo-size", content=b"abcd")
    assert response.status_code == 200
    assert response.json() == {"size": 4}


def test_chunked_body_cannot_bypass_the_limit(monkeypatch):
    monkeypatch.setenv("QUERY_MAX_REQUEST_BODY_BYTES", "4")

    def chunks():
        yield b"abc"
        yield b"de"

    response = TestClient(_app()).post("/echo-size", content=chunks())
    assert response.status_code == 413
