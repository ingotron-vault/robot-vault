"""Robot Vault query service — application assembly.

This module is just the wiring. Where things live:

  - `settings.py`            env-derived config + the Redis cache helpers
  - `auth.py`                OIDC verification, `require_role`, audit, dev tokens
  - `middleware.py`          Prometheus metrics + the CORS/headers/rate-limit stack
  - `planner.py`             the key-pruning query layer (registry -> file list
                             -> DuckDB), source admission, `validate_bucket`
  - `routers/query_api.py`   the lake, the uploads index
  - `routers/fleet.py`       the robot registry + query-time upload aggregates
  - `routers/admin_api.py`   ingest status (deliberately no GC —
                             tools/s3_gc.py is the only collector)
  - `routers/obs.py`         bounded Prometheus API and KPI endpoints
  - `s3api.py`, `uploads.py`, `registry.py`, `time_index_cache.py`

Episodes, dataset cuts and signing are Pro (the Pro package); core
mounts them only through `robot_vault_config.edition.load_pro`.

What is left here is what genuinely belongs to the app object: the FastAPI
instance, the static UI mount, the router includes, and the process-level
probes (`/healthz`, `/readyz`, `/metrics`, `/api/selfcheck`).

The assembly is `create_app()` so the app is explicitly a
function of the settings that shape it -- today that is `ROBOT_VAULT_EDITION`
(see the factory's docstring). The handlers below stayed at module level and
are wired in `_register_process_routes`: that keeps "which routes exist"
readable in one place. `app = create_app()` at the bottom provides
`query_service.main:app`, which uvicorn and the Dockerfile's CMD name.
"""
from __future__ import annotations

import asyncio
import secrets
import logging
import os
from pathlib import Path
from typing import Dict

import duckdb
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from robot_vault_config import edition as edition_mod
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth as auth_mod
from . import mcp as mcp_mod
from . import middleware as middleware_mod
from . import planner
from . import registry as registry_mod
from . import s3api as s3api_mod
from . import settings
from .routers import admin_api, fleet, obs, query_api, robots_admin
from . import download_tickets, robot_bootstrap
from .time_index_cache import initialize_time_index_cache

logger = logging.getLogger(__name__)

# Initialize the ReductStore-inspired time-indexed cache before any router that
# reads it is imported/used.
initialize_time_index_cache(settings.TIME_INDEX_CACHE_SIZE_MB)


def _is_spa_route(path: str) -> bool:
    """True for a /ui path the SPA router owns: not an asset, not a file."""
    if not path.startswith("/ui/"):
        return False
    rest = path[len("/ui/"):]
    if rest.split("/", 1)[0] in ("assets", "licenses"):
        return False
    return "." not in rest.rsplit("/", 1)[-1]


def create_app() -> FastAPI:
    """Assemble the application for the edition this process is running.

    In `core`, the Pro component is never imported, so its
    paths 404 and are absent from `/openapi.json` -- a core box does not
    merely refuse the pro API, it does not document or advertise one.

    A mount-time conditional is the right shape *here* and the wrong shape for
    `AUTH_MODE`. The edition is a deployment property: it is fixed for the
    life of the container, changing it means recreating the service with a
    different env, and the whole point is that the surface itself is absent
    rather than guarded. `AUTH_MODE` is the opposite -- it is read live off
    `settings` on every request (see `require_role`, and the note in
    `tests/unit/test_query_sql_auth.py` about the
    `dependencies=[...] if AUTH_MODE == "oidc" else None` form freezing the
    condition at registration time), because tests and operators flip it
    within a running process and a stale auth decision would be a security
    bug. Freezing the edition at registration time is not a bug, it is the
    requirement.

    Called once at import (`app = create_app()`); tests call it again with
    `settings.EDITION` monkeypatched to build the other edition's app.
    """
    # A typo in AUTH_MODE must not disable every role dependency. Validate the
    # complete static trust contract before exposing even a health endpoint.
    settings.validate_auth_configuration()
    # None in core (nothing imported); in pro a missing or mismatched Pro
    # package refuses startup here.
    pro = edition_mod.load_pro("query", settings.EDITION)

    app = FastAPI(
        title="Robot Vault Query Service",
        version=settings.SERVICE_VERSION,
    )
    app.state.pro = pro
    app.state.identity = edition_mod.identity(settings.EDITION)

    @app.exception_handler(planner.QueryExecutionTimeout)
    async def _query_execution_timeout(
        _request: Request, _exc: planner.QueryExecutionTimeout
    ) -> JSONResponse:
        return JSONResponse(
            status_code=504,
            content={"detail": {"code": "query_timeout"}},
        )

    middleware_mod.install(app)

    static_root = Path(__file__).parent.parent / "ui"
    app.mount("/ui", StaticFiles(directory=str(static_root), html=True), name="ui")
    spa_index = static_root / "index.html"

    # Orbit routes in the browser, so a reload or deep link to an in-app page
    # without a prebuilt directory index (/ui/help, a stale or mistyped URL)
    # gets the SPA entry, whose router shows the page or its not-found
    # recovery links. A missing asset or licence file, and every non-UI path,
    # keeps the ordinary 404.
    @app.exception_handler(StarletteHTTPException)
    async def _spa_route_fallback(request: Request, exc: StarletteHTTPException) -> Response:
        if (
            exc.status_code == 404
            and request.method in ("GET", "HEAD")
            and _is_spa_route(request.url.path)
            and spa_index.is_file()
        ):
            return FileResponse(spa_index, media_type="text/html")
        return await http_exception_handler(request, exc)

    # Every data-bearing router has a dynamic viewer floor. `require_role`
    # reads AUTH_MODE at request time, so this remains a no-op for explicitly
    # unauthenticated local deployments without freezing a fail-open decision
    # at registration time. Route-specific operator/admin dependencies remain
    # additive. The only public HTTP surface is the SPA/auth bootstrap plus
    # the explicit process probes and robot delivery routes registered below.
    viewer_guard = [Depends(auth_mod.require_role("viewer"))]
    app.include_router(download_tickets.router)
    app.include_router(s3api_mod.router, dependencies=viewer_guard)
    app.include_router(query_api.router, dependencies=viewer_guard)
    app.include_router(fleet.router, dependencies=viewer_guard)
    if pro is not None:
        # The Pro routes. Absent -- not 403'd, not empty -- in core.
        pro.register(app, viewer_guard)
    app.include_router(admin_api.router, dependencies=viewer_guard)
    # Browser admin actions cross to ingest only through this narrow
    # token-scoped proxy; ingest's port 8080 remains private to Compose.
    app.include_router(robots_admin.router, dependencies=viewer_guard)
    app.include_router(obs.router, dependencies=viewer_guard)
    app.include_router(auth_mod.router)
    _register_process_routes(app)
    # /mcp. Last, so its gateway is the outermost layer (docs/MCP.md).
    mcp_mod.install(app, pro)
    return app


def _register_process_routes(app: FastAPI) -> None:
    """Wire the process-level probes onto `app`.

    `app.get(path)(handler)` is the same registration the `@app.get(path)`
    decorator performs; spelling it this way is what lets the handlers stay
    module-level functions while the app itself is built by a factory.
    Registration order: routers first, then these.
    """
    app.get("/")(root)
    app.get("/healthz")(healthz)
    app.get("/readyz")(readyz)
    # Robot onboarding material: unauthenticated and un-guarded, because a
    # robot has no identity yet. All of it is public (a CA certificate, the
    # bootstrap script, the agent build); robots verify each piece before use.
    app.get("/ca.crt")(ca_certificate)
    app.get("/robot")(robot_bootstrap.robot_script)
    app.get("/robot/agent-linux-amd64.tar.gz")(robot_bootstrap.robot_agent_tarball)
    app.get("/metrics")(metrics)
    viewer_guard = [Depends(auth_mod.require_role("viewer"))]
    app.get("/api/tenant", dependencies=viewer_guard)(get_tenant)
    app.get("/api/v1/edition")(edition)
    app.get(
        "/api/selfcheck",
        dependencies=[Depends(auth_mod.require_role("operator"))],
    )(selfcheck)


def root() -> HTMLResponse:
    return HTMLResponse("<html><body>See <a href='/ui'>/ui</a></body></html>")


def healthz():
    return {"status": "ok"}


def _postgres_is_ready() -> bool:
    """Execute a bounded, side-effect-free database readiness query.

    Postgres is optional in legacy/local-filesystem mode. It is mandatory in a
    release, and whenever DATABASE_URL is configured a broken connection must
    make the public readiness endpoint fail rather than only the Compose-local
    healthcheck.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return not settings.RELEASE_MODE
    try:
        with psycopg.connect(
            dsn,
            connect_timeout=3,
            options="-c statement_timeout=3000",
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
    except Exception as exc:
        logger.warning("Postgres readiness check failed (%s)", type(exc).__name__)
        return False


async def readyz(request: Request):
    """Readiness includes OIDC, Postgres, and any edition checks.

    Liveness deliberately does not contact the identity provider: an IdP
    outage should make this replica unready, not trigger a restart storm.
    """
    # An edition may supply extra checks; core reports `dataset_signing` as
    # true so the response shape is the same in both editions. (A comment, not the
    # docstring: the docstring is the OpenAPI description.)
    pro = request.app.state.pro
    pro_checks = (
        pro.readiness_checks() if pro is not None else {"dataset_signing": lambda: True}
    )
    oidc_ready, postgres_ready, *pro_ready = await asyncio.gather(
        asyncio.to_thread(auth_mod.warm_real_oidc),
        asyncio.to_thread(_postgres_is_ready),
        *(asyncio.to_thread(check) for check in pro_checks.values()),
    )
    checks = {
        "oidc": oidc_ready,
        "postgres": postgres_ready,
        **dict(zip(pro_checks, pro_ready)),
    }
    if not (postgres_ready and oidc_ready is True and all(ready is True for ready in pro_ready)):
        return JSONResponse(
            {"status": "not_ready", "checks": checks},
            status_code=503,
        )
    return {"status": "ready", "checks": checks}


# A CA certificate is a few kilobytes. The bound is here so a misconfigured
# path cannot turn an unauthenticated route into an arbitrary file read of
# unbounded size.
CA_CERT_MAX_BYTES = 256 * 1024


def ca_certificate() -> Response:
    """Serve the ingest CA verbatim; robots check its SPKI pin from the code.

    Publishing a CA certificate discloses nothing: it is a public key and a
    subject name. What would disclose something is serving the *wrong* file,
    so this refuses anything that is not a certificate and anything that
    contains a private key -- a misconfigured `INGEST_CA_CERT_FILE` becomes a
    503, never a key leak.
    """
    unavailable = JSONResponse(
        {"error": "ca_certificate_unavailable"}, status_code=503
    )
    path = (settings.INGEST_CA_CERT_FILE or "").strip()
    if not path:
        return unavailable
    try:
        with open(path, "rb") as handle:
            pem = handle.read(CA_CERT_MAX_BYTES + 1)
    except OSError as exc:
        # Never the path or the OS message: this route is unauthenticated.
        logger.warning("Ingest CA certificate unreadable (%s)", type(exc).__name__)
        return unavailable
    if len(pem) > CA_CERT_MAX_BYTES:
        logger.warning("Ingest CA certificate exceeds %d bytes", CA_CERT_MAX_BYTES)
        return unavailable
    if b"-----BEGIN CERTIFICATE-----" not in pem or b"PRIVATE KEY" in pem:
        logger.warning("Configured ingest CA file is not a certificate")
        return unavailable
    return Response(
        content=pem,
        media_type="application/x-pem-file",
        headers={"Cache-Control": "no-store"},
    )


def metrics(request: Request):
    expected = settings.METRICS_AUTH_TOKEN
    if expected:
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="metrics_auth_required",
                headers={"WWW-Authenticate": "Bearer"},
            )
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


def get_tenant():
    return {"scope": settings.S3_PREFIX_SCOPE}


def edition(request: Request):
    """Which product edition this deployment is running.

    Open, like `/healthz`: it discloses nothing a caller cannot already infer
    from which routes exist, and both the SPA (to hide pro-only navigation
    before the user clicks into a 404) and support ("which edition is this
    box?") need one cheap, unauthenticated answer. Present in BOTH editions --
    a core box that 404'd this too would leave the SPA unable to tell "core"
    apart from "old build".
    """
    # The body is `robot_vault_config.edition.identity()` taken when the app
    # was built: `edition`, `core_version`, `adapter_api`, `pro_version` and
    # `capabilities`. The UI footer reads `core_version`. `mcp` is this
    # service's own MCP config, not part of the shared identity.
    return {**request.app.state.identity, "mcp": mcp_mod.status(request.app)}


def selfcheck():
    """Operator diagnostic without exposing raw backend exception text."""
    ok = True
    details: Dict[str, object] = {}
    con = None
    try:
        con = duckdb.connect(database=":memory:")
        con.execute("SELECT 1;")
        details["duckdb"] = "ok"
    except Exception as exc:
        ok = False
        details["duckdb"] = "unavailable"
        logger.warning("DuckDB self-check failed (%s)", type(exc).__name__)
    finally:
        if con is not None:
            con.close()
    # S3-only mode
    if settings.s3_enabled():
        details["s3_enabled"] = True
        details["s3_bucket"] = settings.S3_PARQUET_BUCKET
        details["s3_lake_root"] = planner.lake_root_uri()
    else:
        details["s3_enabled"] = False
    # The `parquet_files` registry is the only answer to "which
    # files exist". Report how many published files it holds.
    try:
        if not registry_mod.registry_dsn():
            details["registry"] = "not configured"
        else:
            summary = registry_mod.describe_schema()
            details["registry"] = f"ok: {summary.file_count} files"
    except Exception as exc:
        ok = False
        details["registry"] = "unavailable"
        logger.warning("Registry self-check failed (%s)", type(exc).__name__)
    return {"status": "ok" if ok else "error", "details": details}


# The module-level app uvicorn serves (`query_service.main:app`).
app = create_app()
