"""Prometheus metric objects and the HTTP middleware stack.

Split out of `main.py`; behaviour is carried over unchanged.
The metrics are module-level singletons because `prometheus_client`'s default
registry rejects a duplicate registration, so they must be created exactly
once per process -- importing this module is that once.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Gauge, Histogram
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import settings

logger = logging.getLogger(__name__)


class _NoopMetric:
    def labels(self, *args, **kwargs):
        return self

    def inc(self, *args, **kwargs):
        return None

    def dec(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None

    def observe(self, *args, **kwargs):
        return None


if os.environ.get("DISABLE_PROM_METRICS"):
    queries_total = _NoopMetric()
    query_duration = _NoopMetric()
    REQUESTS_TOTAL = _NoopMetric()
    QUERY_EXECUTIONS_TOTAL = _NoopMetric()
    QUERY_ACTIVE = _NoopMetric()
    QUERY_CAPACITY = _NoopMetric()
    QUERY_ADMISSION_REJECTIONS_TOTAL = _NoopMetric()
    QUERY_TIMEOUTS_TOTAL = _NoopMetric()
    UPLOADS_LIST_REQUESTS_TOTAL = _NoopMetric()
    UPLOADS_DETAIL_REQUESTS_TOTAL = _NoopMetric()
    UPLOADS_DETAIL_DURATION_SECONDS = _NoopMetric()
    UPLOADS_DETAIL_ROWS_BUCKETED = _NoopMetric()
    UPLOADS_DETAIL_FILES_BUCKETED = _NoopMetric()
    RATE_LIMIT_REDIS_DEGRADATIONS_TOTAL = _NoopMetric()
else:
    # Metrics (Prometheus)
    queries_total = Counter(
        "query_engine_queries_executed_total",
        "Number of queries executed",
        labelnames=("query_type", "status"),
    )
    query_duration = Histogram(
        "query_engine_duration_seconds",
        "Query execution duration in seconds",
        labelnames=("query_type",),
    )
    REQUESTS_TOTAL = Counter(
        "query_service_requests_total",
        "Total HTTP requests",
        ["path", "method", "status"],
    )
    QUERY_EXECUTIONS_TOTAL = Counter(
        "query_service_query_executions_total",
        "Total query executions",
        ["type"],
    )
    QUERY_ACTIVE = Gauge(
        "query_service_query_active",
        "DuckDB query-capacity leases currently held by requests",
    )
    QUERY_CAPACITY = Gauge(
        "query_service_query_capacity",
        "Configured process-wide DuckDB query-capacity permits",
    )
    QUERY_CAPACITY.set(settings.DUCKDB_QUERY_CONCURRENCY)
    QUERY_ADMISSION_REJECTIONS_TOTAL = Counter(
        "query_service_query_admission_rejections_total",
        "Queries rejected before execution, by bounded admission reason",
        ["reason"],
    )
    QUERY_TIMEOUTS_TOTAL = Counter(
        "query_service_query_timeouts_total",
        "DuckDB executions interrupted at their request deadline",
        ["query_type"],
    )
    # Uploads API metrics
    UPLOADS_LIST_REQUESTS_TOTAL = Counter(
        "uploads_list_requests_total",
        "Total uploads list requests",
        ["status"],
    )
    UPLOADS_DETAIL_REQUESTS_TOTAL = Counter(
        "uploads_detail_requests_total",
        "Total uploads detail requests",
        ["status"],
    )
    UPLOADS_DETAIL_DURATION_SECONDS = Histogram(
        "uploads_detail_duration_seconds",
        "Duration of uploads detail aggregation in seconds",
    )
    UPLOADS_DETAIL_ROWS_BUCKETED = Counter(
        "uploads_detail_rows_total_bucketed",
        "Uploads detail rows_total bucketed counter",
        ["bucket"],
    )
    UPLOADS_DETAIL_FILES_BUCKETED = Counter(
        "uploads_detail_files_scanned_bucketed",
        "Uploads detail files scanned bucketed counter",
        ["bucket"],
    )
    RATE_LIMIT_REDIS_DEGRADATIONS_TOTAL = Counter(
        "query_service_rate_limit_redis_degradations_total",
        "Requests that used the per-process rate-limit fallback after a Redis error",
    )


# Basic in-memory rate limit per client IP + policy bucket + minute window. It
# is the normal single-process limiter and also the fail-closed fallback when
# configured Redis is temporarily unavailable.
_rate_counts: Dict[Tuple[str, str, int], int] = {}
_rate_lock = threading.Lock()
_redis_degradation_lock = threading.Lock()
_redis_degradation_last_logged_at = 0.0
_REDIS_DEGRADATION_LOG_INTERVAL_SECONDS = 60.0
_RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "240"))
_REQUEST_BODY_MAX_BYTES = 8 * 1024 * 1024


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized declared and chunked bodies before handlers run."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except (TypeError, ValueError):
                response = PlainTextResponse("Invalid Content-Length", status_code=400)
                await response(scope, receive, send)
                return
            if declared_bytes < 0:
                response = PlainTextResponse("Invalid Content-Length", status_code=400)
                await response(scope, receive, send)
                return
            if declared_bytes > self.max_bytes:
                response = PlainTextResponse("Request Body Too Large", status_code=413)
                await response(scope, receive, send)
                return

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            response = PlainTextResponse("Request Body Too Large", status_code=413)
            await response(scope, receive, send)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _rate_policy(path: str) -> Tuple[str, int]:
    """Return a bounded policy key and limit, never the attacker-controlled path."""
    if path.startswith("/api/admin/"):
        return "admin", _env_int("RATE_LIMIT_ADMIN_PER_MINUTE", 60)
    if path.startswith("/api/v1/s3/presign"):
        return "presign", _env_int("RATE_LIMIT_PRESIGN_PER_MINUTE", 120)
    return "default", _env_int("RATE_LIMIT_DEFAULT_PER_MINUTE", _RATE_LIMIT_PER_MINUTE)


def _memory_rate_limited(client_ip: str, bucket: str, window: int, limit: int) -> bool:
    key = (client_ip, bucket, window)
    with _rate_lock:
        count = _rate_counts.get(key, 0) + 1
        _rate_counts[key] = count
        if len(_rate_counts) > 1024:
            for old_key in list(_rate_counts):
                if old_key[2] != window:
                    _rate_counts.pop(old_key, None)
    return count > limit


def _redis_increment(client, key: str) -> int:
    """Atomically increment a fixed-window key and ensure it has a TTL."""
    with client.pipeline(transaction=True) as pipeline:
        pipeline.incr(key)
        # NX also repairs a legacy/crash-created key that has no expiry while
        # leaving the original minute window unchanged on normal increments.
        pipeline.expire(key, 60, nx=True)
        current, _ = pipeline.execute()
    return int(current)


def _record_redis_degradation(exc: Exception) -> None:
    """Count every fallback but log at most once per interval, without secrets."""
    global _redis_degradation_last_logged_at
    RATE_LIMIT_REDIS_DEGRADATIONS_TOTAL.inc()
    now = time.monotonic()
    should_log = False
    with _redis_degradation_lock:
        if (
            _redis_degradation_last_logged_at == 0.0
            or now - _redis_degradation_last_logged_at
            >= _REDIS_DEGRADATION_LOG_INTERVAL_SECONDS
        ):
            _redis_degradation_last_logged_at = now
            should_log = True
    if should_log:
        logger.warning(
            "Redis rate limiter unavailable (%s); using the per-process fallback",
            type(exc).__name__,
        )


def install(app: FastAPI) -> None:
    """Attach the bounded HTTP middleware stack.

    Starlette runs middleware in reverse registration order, so this must stay
    one ordered installer rather than scattered registrations.
    """
    # Register first so this pure-ASGI limiter sits immediately outside the
    # router/exception layer. The BaseHTTP middlewares registered below wrap
    # it; they can add security headers and metrics to a 413 without wrapping
    # a receive-side limit exception in an ExceptionGroup.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=_env_int("QUERY_MAX_REQUEST_BODY_BYTES", _REQUEST_BODY_MAX_BYTES),
    )

    # Browser API calls are same-origin in the shipped UI. Keep only explicit
    # development origins for the separate Vite server; operators can append
    # trusted origins without turning the bearer-token API into a wildcard
    # cross-origin resource.
    cors_origins = [
        origin.strip()
        for origin in os.environ.get(
            "CORS_ALLOWED_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip() and origin.strip() != "*"
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Security headers
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if settings.ENABLE_HSTS and request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # CSP. Vite emits external hashed scripts, so inline script execution
        # is not needed (and would amplify any injection into session-held
        # bearer tokens). Inline styles remain because the shipped charts and
        # progress bars calculate dimensions with React style attributes.
        # A real-OIDC SPA exchanges its PKCE code and refresh token directly
        # with the configured IdP.  Permit exactly that validated HTTPS origin,
        # never a wildcard; invalid/incomplete auth config stays self-only.
        connect_sources = ["'self'"]
        try:
            token_origin = settings.browser_oidc_token_origin()
            if token_origin:
                connect_sources.append(token_origin)
        except ValueError:
            pass
        csp = "; ".join([
            "default-src 'self'",
            "base-uri 'none'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            # Authenticated, size-bounded Review downloads play through Blob URLs.
            "media-src 'self' blob:",
            f"connect-src {' '.join(connect_sources)}",
            "object-src 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ])
        response.headers.setdefault("Content-Security-Policy", csp)
        # Vite content-hashes /ui/assets names. Cache those immutable bytes for
        # a year while keeping entry HTML and the OIDC callback uncached.
        if request.url.path.startswith("/ui/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            if "Pragma" in response.headers:
                del response.headers["Pragma"]
        elif request.url.path.startswith("/ui"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    # Redis-backed rate limiter (fixed window per-IP) with path-specific limits
    @app.middleware("http")
    async def redis_rate_limit(request: Request, call_next):
        if not settings._redis:
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        window = int(time.time() // 60)
        path = request.url.path or ""

        bucket, limit = _rate_policy(path)
        key = f"rl:{client_ip}:{bucket}:{window}"
        try:
            cur = await run_in_threadpool(_redis_increment, settings._redis, key)
            if cur > limit:
                return PlainTextResponse("Too Many Requests", status_code=429)
        except Exception as exc:
            # A configured-but-unreachable Redis used to disable both Redis
            # and memory limiting. Preserve availability without failing open.
            _record_redis_degradation(exc)
            if _memory_rate_limited(client_ip, bucket, window, limit):
                return PlainTextResponse("Too Many Requests", status_code=429)
        return await call_next(request)

    @app.middleware("http")
    async def simple_rate_limit(request: Request, call_next):
        if settings._redis:
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        window = int(time.time() // 60)
        bucket, limit = _rate_policy(request.url.path or "")
        if _memory_rate_limited(client_ip, bucket, window, limit):
            return PlainTextResponse("Too Many Requests", status_code=429)
        return await call_next(request)

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        response: Response = await call_next(request)
        try:
            # Label with the matched ROUTE TEMPLATE, not the raw path. Several
            # routes carry high-cardinality segments
            # (/api/v1/uploads/{upload_id}/detail,
            # /api/v1/robots/{robot_id}/data/{message_type:path}), so labelling
            # by raw path creates one Prometheus time series per upload id, per
            # robot and per topic -- unbounded growth in the scrape payload and
            # in the TSDB. 404s collapse into a single "unmatched" bucket rather
            # than minting a series per scanned URL.
            route = request.scope.get("route")
            path_label = getattr(route, "path_format", None) or getattr(route, "path", None) or "unmatched"
            REQUESTS_TOTAL.labels(
                path=path_label,
                method=request.method,
                status=str(response.status_code),
            ).inc()
        except Exception:
            pass
        return response
