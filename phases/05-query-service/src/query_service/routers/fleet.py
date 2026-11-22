"""Fleet endpoints: the robot registry with query-time upload aggregates.

The registry is in `001_baseline.sql` (`id` PK, `name`, `created_at`,
JSONB `metadata`, plus a real FK from `uploads.robot_id`). The response maps
those columns to the shape the SPA consumes (`robot_id` / `display_name` /
`first_seen`).

The registry deliberately stores identity only. Every statistic here is
computed at query time by joining `uploads`, so nothing can drift out of sync
with the system of record.
"""
from __future__ import annotations

import os

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from .. import auth as auth_mod
from .. import settings

router = APIRouter(tags=["fleet"])


# One row per registered robot, LEFT JOINed against `uploads` for the
# aggregates. `COUNT(u.id)` rather than `COUNT(*)` so a robot with no uploads
# reads as 0 (the LEFT JOIN yields one all-NULL `u.*` row), and `MAX(...)` over
# that same row is NULL, i.e. "never uploaded".
_ROBOTS_AGGREGATE_SQL = """
    SELECT
        r.id,
        r.name,
        r.created_at,
        COUNT(u.id) AS total_uploads,
        COUNT(*) FILTER (WHERE u.status = 'completed') AS completed_uploads,
        COUNT(*) FILTER (WHERE u.status = 'failed') AS failed_uploads,
        COUNT(*) FILTER (WHERE u.status = 'uploading') AS uploading,
        COALESCE(SUM(u.file_size_bytes), 0) AS total_bytes,
        MAX(COALESCE(u.upload_completed_at, u.created_at)) AS last_upload_at
    FROM robots r
    LEFT JOIN uploads u ON u.robot_id = r.id
    GROUP BY r.id, r.name, r.created_at
    ORDER BY r.id
"""

_ROBOT_AGGREGATE_BY_ID_SQL = """
    SELECT
        r.id,
        r.name,
        r.created_at,
        COUNT(u.id) AS total_uploads,
        COUNT(*) FILTER (WHERE u.status = 'completed') AS completed_uploads,
        COUNT(*) FILTER (WHERE u.status = 'failed') AS failed_uploads,
        COUNT(*) FILTER (WHERE u.status = 'uploading') AS uploading,
        COALESCE(SUM(u.file_size_bytes), 0) AS total_bytes,
        MAX(COALESCE(u.upload_completed_at, u.created_at)) AS last_upload_at
    FROM robots r
    LEFT JOIN uploads u ON u.robot_id = r.id
    WHERE r.id = %s
    GROUP BY r.id, r.name, r.created_at
"""

_ROBOT_TOPICS_SQL = """
    SELECT pt.topic, COUNT(DISTINCT pt.upload_id) AS uploads, SUM(pt.rows_written) AS rows_written
    FROM processed_topics pt
    JOIN uploads u ON u.id = pt.upload_id
    JOIN processed published ON published.upload_id = pt.upload_id
    WHERE u.robot_id = %s
      AND pt.attempt_id = published.attempt_id
    GROUP BY pt.topic
    ORDER BY pt.topic
"""

_ROBOT_RECENT_UPLOADS_SQL = """
    SELECT id::text AS upload_id, original_filename, status, file_size_bytes, created_at
    FROM uploads
    WHERE robot_id = %s
    ORDER BY created_at DESC
    LIMIT 5
"""


def _row_to_item(row) -> dict:
    (robot_id, name, created_at, total_uploads, completed_uploads,
     failed_uploads, uploading, total_bytes, last_upload_at) = row
    return {
        "robot_id": robot_id,
        "display_name": name,
        "first_seen": settings.iso_utc(created_at),
        "last_upload_at": settings.iso_utc(last_upload_at),
        "total_uploads": int(total_uploads or 0),
        "completed_uploads": int(completed_uploads or 0),
        "failed_uploads": int(failed_uploads or 0),
        "uploading": int(uploading or 0),
        "total_bytes": int(total_bytes or 0),
    }


@router.get("/api/v1/robots/list")
def robots_list(_user=Depends(auth_mod.require_role("viewer"))):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return {"items": []}
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_ROBOTS_AGGREGATE_SQL)
                rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        # `robots`/`uploads` not created yet -- an un-migrated DB genuinely has
        # no fleet. Every OTHER psycopg error (refused, auth, DNS, bad DSN) is a
        # real outage and must propagate as a 500 rather than read as "empty",
        # which is the error-vs-empty rule.
        return {"items": []}
    return {"items": [_row_to_item(r) for r in rows]}


@router.get("/api/v1/robots/{robot_id}/summary")
def robot_summary(robot_id: str, _user=Depends(auth_mod.require_role("viewer"))):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        # Cannot confirm the robot exists either way; do not fabricate stats.
        raise HTTPException(status_code=404, detail="unknown robot")
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_ROBOT_AGGREGATE_BY_ID_SQL, (robot_id,))
                row = cur.fetchone()
                if row is None:
                    # The baseline schema has an FK from uploads.robot_id, so an
                    # upload for an unregistered robot cannot exist -- absent
                    # from `robots` means absent, full stop.
                    raise HTTPException(status_code=404, detail="unknown robot")
                item = _row_to_item(row)

                cur.execute(_ROBOT_TOPICS_SQL, (robot_id,))
                item["topics"] = [
                    {"topic": t, "uploads": int(n or 0), "rows_written": int(rw or 0)}
                    for (t, n, rw) in cur.fetchall()
                ]

                cur.execute(_ROBOT_RECENT_UPLOADS_SQL, (robot_id,))
                item["recent_uploads"] = [
                    {
                        "upload_id": upload_id,
                        "original_filename": original_filename,
                        "status": status,
                        "file_size_bytes": int(file_size_bytes or 0),
                        "created_at": settings.iso_utc(created_at),
                    }
                    for (upload_id, original_filename, status, file_size_bytes, created_at)
                    in cur.fetchall()
                ]
    except psycopg.errors.UndefinedTable:
        raise HTTPException(status_code=404, detail="unknown robot")

    return item
