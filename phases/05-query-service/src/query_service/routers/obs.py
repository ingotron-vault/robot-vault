"""Observability: bounded Prometheus queries, profile health, and
the Home page's KPI tiles.

Split out of `main.py`. There is no Grafana proxy: `/ui/obs` covers the
same panels. Prometheus stays as an opt-in advanced tool behind the `obs` compose profile; the KPI handler
below is the one exception to "pure pass-through to the `obs` profile" --
Home's tiles must be populated in every deployment, `obs` profile or not, so
they read Postgres directly instead.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Dict, Optional

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException

from .. import settings
from ..auth import require_role

router = APIRouter()

_PROMETHEUS_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
_PROMQL_MAX_CHARS = 4096
_PROMQL_MAX_STEP_CHARS = 16
_PROMQL_MAX_RANGE_SECONDS = 31 * 24 * 60 * 60
_PROMQL_MAX_POINTS = 11_000
_STEP = re.compile(r"^([1-9][0-9]*)(ms|s|m|h|d|w|y)$")
_STEP_MULTIPLIERS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 7 * 86400.0,
    "y": 365 * 86400.0,
}


def _prometheus_base() -> str:
    return settings.PROMETHEUS_BASE


def _step_seconds(step: str) -> float:
    if len(step) > _PROMQL_MAX_STEP_CHARS:
        raise HTTPException(status_code=400, detail="invalid_step")
    match = _STEP.fullmatch(step)
    if not match:
        raise HTTPException(status_code=400, detail="invalid_step")
    return int(match.group(1)) * _STEP_MULTIPLIERS[match.group(2)]


async def _bounded_prometheus_json(path: str, *, params=None, timeout: float) -> object:
    """Read one allowlisted Prometheus API response with a hard byte cap."""
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=20.0) as client:
            async with client.stream(
                "GET",
                f"{_prometheus_base().rstrip('/')}/{path.lstrip('/')}",
                params=params,
                timeout=timeout,
            ) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(status_code=502, detail="prometheus_upstream_error")
                chunks = bytearray()
                # HTTPX advertises compression by default. Bound the decoded
                # bytes that JSON will actually consume; aiter_raw() would
                # hand gzip/deflate payloads directly to json.loads().
                async for chunk in upstream.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _PROMETHEUS_RESPONSE_MAX_BYTES:
                        raise HTTPException(
                            status_code=502,
                            detail="prometheus_response_too_large",
                        )
                try:
                    return json.loads(chunks)
                except (TypeError, ValueError, UnicodeDecodeError) as exc:
                    raise HTTPException(
                        status_code=502,
                        detail="invalid_prometheus_response",
                    ) from exc
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="prometheus_unavailable") from exc


@router.get("/api/obs/promql/range")
async def promql_range(
    query: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    step: str = "30s",
    _user=Depends(require_role("operator")),
):
    """Run one PromQL range query against Prometheus.

    - query: PromQL expression
    - start/end: unix seconds; defaults to last 1h when not provided
    - step: Prometheus step duration (e.g., 15s, 1m)

    Operator or admin only. The OBS page sends free-form PromQL from its
    ad-hoc explorer, so an allowlist of fixed queries cannot cover it.
    Prometheus holds every scraped label of every service, which is more
    than the viewer role should see. The router's viewer floor still runs
    first, so an unauthenticated caller gets 401 and a viewer gets 403.
    """
    if not query or len(query) > _PROMQL_MAX_CHARS:
        raise HTTPException(status_code=400, detail="invalid_promql")
    step_seconds = _step_seconds(step)
    now = time.time()
    if end is None:
        end = now
    if start is None:
        start = end - 3600
    if not math.isfinite(start) or not math.isfinite(end):
        raise HTTPException(status_code=400, detail="invalid_range")
    range_seconds = end - start
    if range_seconds < 0 or range_seconds > _PROMQL_MAX_RANGE_SECONDS:
        raise HTTPException(status_code=400, detail="invalid_range")
    if math.floor(range_seconds / step_seconds) + 1 > _PROMQL_MAX_POINTS:
        raise HTTPException(status_code=400, detail="too_many_points")
    params = {
        "query": query,
        "start": str(float(start)),
        "end": str(float(end)),
        "step": step,
    }
    return await _bounded_prometheus_json(
        "api/v1/query_range",
        params=params,
        timeout=15.0,
    )


@router.get("/api/obs/alerts")
async def prom_alerts():
    """Return active Prometheus alerts (via Alertmanager-compatible endpoint exposed by Prometheus)."""
    return await _bounded_prometheus_json("api/v1/alerts", timeout=10.0)


@router.get("/api/obs/health")
async def obs_health():
    """Health of the `obs` profile itself -- used by the SPA to decide
    whether the OBS nav link (and its Prometheus-backed panels) has anywhere
    to point. Grafana's leg was removed along with the proxy; Prometheus is
    now the only thing "the obs profile" means.
    """
    status: Dict[str, object] = {"prometheus": None}
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=5.0) as client:
            # Health needs only the response status. Streaming without reading
            # the body prevents a broken/compromised upstream from making this
            # endpoint buffer an arbitrarily large diagnostic payload.
            async with client.stream(
                "GET",
                f"{_prometheus_base().rstrip('/')}/-/healthy",
            ) as upstream:
                status["prometheus"] = {
                    "ok": upstream.status_code == 200,
                    "status": upstream.status_code,
                }
    except httpx.HTTPError:
        status["prometheus"] = {"ok": False, "error": "unavailable"}
    return {"status": status}


# --- Home's KPI tiles --------------------------------------------------------
#
# Postgres-backed, not Prometheus-backed: routing Home's tiles through
# Prometheus would make them read as "down" whenever the opt-in `obs` profile simply wasn't running -- which is
# the common case. These four numbers come straight from the system of
# record instead, so they are populated in every deployment. Modeled on
# `routers/fleet.py`'s idiom: no DATABASE_URL or an UndefinedTable (an
# un-migrated DB, or -- for `episodes_awaiting_qa` -- a core deployment that
# never created the pro-only `episodes`/`qa_reports` tables) both mean "no
# data", i.e. null, never a fabricated number. Any OTHER psycopg error
# (refused, auth, DNS, bad DSN) propagates as a 500, matching fleet.py and
# `/api/v1/robots/list`.
_KPI_QUERIES: Dict[str, str] = {
    "uploads_last_hour": "SELECT count(*) FROM uploads WHERE created_at > now() - interval '1 hour'",
    "storage_bytes_total": "SELECT COALESCE(sum(file_size_bytes), 0) FROM uploads WHERE status = 'completed'",
    "episodes_awaiting_qa": (
        "SELECT count(*) FROM episodes e "
        "JOIN processed published ON published.upload_id = e.upload_id "
        "WHERE e.attempt_id = published.attempt_id "
        "AND NOT EXISTS (SELECT 1 FROM qa_reports q "
        "WHERE q.episode_id = e.episode_id "
        "AND q.attempt_id = published.attempt_id)"
    ),
    "exports_completed_24h": (
        "SELECT count(*) FROM export_status WHERE status = 'completed' "
        "AND completed_at > now() - interval '24 hours'"
    ),
}


@router.get("/api/v1/metrics/overview")
def metrics_overview():
    """Home page KPI tiles: uploads in the last hour, total stored bytes,
    items awaiting QA, and exports completed in the last 24h -- all read
    from Postgres at request time, so they are always populated.

    Mounted in both editions (this router carries no `is_pro()` gate), so
    unlike the module comment above, this docstring must stay clear of the
    pro-only vocabulary behind some of these numbers -- see
    `test_edition_gate.py`'s check on a core deployment's `/openapi.json`.
    """
    results: Dict[str, Optional[int]] = {k: None for k in _KPI_QUERIES}
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return {"kpis": results}
    try:
        # Bound both the connect and every statement: this endpoint runs on a
        # threadpool worker (sync `def` route) and is polled every 30s per open
        # Home tab. Without these bounds a slow/blackholed Postgres would pin a
        # worker for the full default TCP timeout per poll and, in aggregate,
        # starve every other sync route. Mirrors admin_api.py / main.py.
        with psycopg.connect(
            dsn,
            connect_timeout=3,
            options="-c statement_timeout=3000",
        ) as conn:
            with conn.cursor() as cur:
                for key, sql in _KPI_QUERIES.items():
                    try:
                        cur.execute(sql)
                        value = cur.fetchone()[0]
                        results[key] = int(value) if value is not None else None
                    except psycopg.errors.UndefinedTable:
                        # `episodes`/`qa_reports` absent in a core edition, or
                        # an un-migrated DB -- "no data", not an outage.
                        # Each KPI is independent: one missing table must not
                        # blank out the other three, so roll back just this
                        # query's aborted transaction and continue.
                        conn.rollback()
                        results[key] = None
    except psycopg.errors.UndefinedTable:
        return {"kpis": {k: None for k in _KPI_QUERIES}}
    return {"kpis": results}
