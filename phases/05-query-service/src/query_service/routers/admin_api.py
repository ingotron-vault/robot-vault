"""Operator endpoint for inspecting ingest status.

Every route here is role-gated (`operator`) and audit-logged.

This router deliberately exposes NO garbage-collection endpoint -- see the
block comment at the bottom of the file, and
phases/05-query-service/tests/unit/test_single_gc_invariant.py, which fails
if one reappears here or anywhere else outside tools/s3_gc.py.
"""
from __future__ import annotations

import logging
import os
from typing import Dict

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import settings
from ..auth import audit, require_role

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/admin/ingest/status")
def admin_ingest_status(
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    _=Depends(require_role("operator")),
):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise HTTPException(status_code=503, detail="db_not_configured")
    data: Dict[str, object] = {}
    try:
        with psycopg.connect(
            dsn,
            connect_timeout=3,
            options="-c statement_timeout=3000",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) FROM uploads GROUP BY 1")
                data["uploads_counts"] = {str(r[0]): int(r[1]) for r in cur.fetchall()}
                cur.execute(
                    """
                    SELECT client_upload_id, robot_id, original_filename, status,
                           upload_started_at, upload_completed_at
                    FROM uploads ORDER BY created_at DESC LIMIT %s
                    """,
                    (int(limit),),
                )
                data["recent_uploads"] = [
                    {
                        "upload_id": str(r[0] or ""),
                        "robot_id": str(r[1] or ""),
                        "filename": str(r[2] or ""),
                        "status": str(r[3] or ""),
                        "started_at": settings.iso_utc(r[4]),
                        "completed_at": settings.iso_utc(r[5]),
                    }
                    for r in cur.fetchall()
                ]
                cur.execute("SELECT status, COUNT(*) FROM processing_status GROUP BY 1")
                data["processing_counts"] = {str(r[0]): int(r[1]) for r in cur.fetchall()}
                cur.execute("SELECT COUNT(*) FROM processed")
                data["processed_count"] = int(cur.fetchone()[0])
                # The fleet registry is the anchor for robot
                # identity, so an operator looking at ingest health should see
                # it next to the upload counts.
                cur.execute("SELECT COUNT(*) FROM robots")
                data["robots_count"] = int(cur.fetchone()[0])
    except Exception as exc:
        logger.error(
            "Admin ingest status database query failed (%s)",
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="database_unavailable") from exc
    try:
        audit(request, "admin_ingest_status", {"limit": int(limit)})
    except Exception as exc:
        logger.warning(
            "Could not write admin ingest status audit event (%s)",
            type(exc).__name__,
        )
    return data


# ---------------------------------------------------------------------------
# NOT HERE: POST /api/admin/gc/dry-run and POST /api/admin/gc/execute.
#
# There is deliberately no GC endpoint. Reachability is a whole-system
# invariant, and a copy of it behind an HTTP verb has no owner, no metrics and
# no dry-run default, and drifts from the real one. tools/s3_gc.py is the ONLY
# implementation and the ONLY thing in the system that deletes S3 objects; it
# runs as its own `gc` compose service, is guarded, metered (gc_* Prometheus
# metrics on :9108), dry-run-gated via GC_DRY_RUN, and covered by
# tests/integration/test_gc_reachability.py plus the source/AST invariants in
# phases/05-query-service/tests/unit/test_single_gc_invariant.py.
#
# Operators drive GC through the `gc` service's config (configs/gc.yaml +
# GC_* env overrides, see docs/CONFIG_REFERENCE.md), not through this API.
# ---------------------------------------------------------------------------
