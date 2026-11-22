"""Shared Postgres fencing primitives for processor publication.

Object storage cannot participate in the Postgres transaction that owns a
processing lease.  Writers therefore publish bytes to attempt-specific keys
and call :func:`lock_processing_claim` in the transaction that registers
those bytes.  Losing the lease can leave an unregistered object for GC, but
can never make stale metadata visible or mutate the replacement attempt.
"""

from __future__ import annotations

import uuid


# The first statement changes the processing_status token and takes the row
# lock. Cleanup deliberately runs as later statements in the SAME transaction
# (see ``prune_processing_claims``): under READ COMMITTED each gets a fresh
# snapshot, so it sees a predecessor registry transaction that committed while
# this claim waited for the row lock. Same-statement data-modifying CTEs would
# retain the snapshot taken before that wait and could miss exactly those
# crossing writes. Keeping both pieces here lets the live Postgres concurrency
# test exercise production SQL without importing PyArrow, ROS, or the rest of
# the processor runtime.
PROCESSING_CLAIM_SQL = """
WITH attempt_limit AS (
    SELECT %s::integer AS max_attempts
), exhausted AS (
    -- A killed worker cannot record its own final failure. Terminalize its
    -- expired claim without granting another attempt. UPDATE rechecks the
    -- predicate after waiting for a concurrent heartbeat/completion row lock.
    UPDATE processing_status
    SET status = 'dead_letter',
        completed_at = NOW(),
        error_message = 'Processing lease expired after the final allowed attempt',
        lease_expires_at = NULL,
        claimed_by = NULL,
        updated_at = NOW()
    WHERE status = 'processing'
      AND attempts >= (SELECT max_attempts FROM attempt_limit)
      AND COALESCE(lease_expires_at, '-infinity'::timestamptz) <= NOW()
), candidates AS (
    SELECT u.id, u.client_upload_id, u.robot_id, u.storage_path
    FROM uploads u
    LEFT JOIN processed p ON p.upload_id = u.id
    LEFT JOIN processing_status ps ON ps.upload_id = u.id
    WHERE u.status = 'completed'
      AND u.integrity_verified IS TRUE
      AND p.upload_id IS NULL
      AND COALESCE(ps.attempts, 0) < (SELECT max_attempts FROM attempt_limit)
      AND NOT (u.client_upload_id = ANY(%s::text[]))
      AND (
          ps.upload_id IS NULL
          OR ps.status IN ('pending', 'failed')
          OR (
              ps.status = 'processing'
              AND COALESCE(
                  ps.lease_expires_at, '-infinity'::timestamptz
              ) <= NOW()
          )
      )
    ORDER BY u.upload_completed_at DESC
    LIMIT %s
), claimed AS (
    INSERT INTO processing_status
        (upload_id, status, attempts, started_at, completed_at,
         error_message, metadata, attempt_id, claimed_by, lease_expires_at)
    SELECT
        c.id, 'processing', 1, NOW(), NULL, NULL, NULL,
        gen_random_uuid(), %s, NOW() + (%s * INTERVAL '1 second')
    FROM candidates c
    ON CONFLICT (upload_id) DO UPDATE SET
        status = 'processing',
        attempts = processing_status.attempts + 1,
        started_at = NOW(),
        completed_at = NULL,
        error_message = NULL,
        metadata = NULL,
        attempt_id = gen_random_uuid(),
        claimed_by = EXCLUDED.claimed_by,
        lease_expires_at = EXCLUDED.lease_expires_at,
        updated_at = NOW()
    WHERE processing_status.attempts < %s
      AND (
          processing_status.status IN ('pending', 'failed')
          OR (
              processing_status.status = 'processing'
              AND COALESCE(
                  processing_status.lease_expires_at,
                  '-infinity'::timestamptz
              ) <= NOW()
          )
      )
    RETURNING upload_id, attempt_id, attempts
)
SELECT c.client_upload_id, c.robot_id, c.storage_path,
       claimed.attempt_id::text, claimed.attempts
FROM candidates c
JOIN claimed ON claimed.upload_id = c.id
"""


PROCESSING_CLAIM_PRUNE_SQL = (
    """
    DELETE FROM qa_reports AS old
    USING processing_status AS claimed
    WHERE claimed.attempt_id = ANY(%s::uuid[])
      AND claimed.status = 'processing'
      AND old.upload_id = claimed.upload_id
      AND old.attempt_id <> claimed.attempt_id
    """,
    """
    DELETE FROM episodes AS old
    USING processing_status AS claimed
    WHERE claimed.attempt_id = ANY(%s::uuid[])
      AND claimed.status = 'processing'
      AND old.upload_id = claimed.upload_id
      AND old.attempt_id <> claimed.attempt_id
    """,
    """
    DELETE FROM parquet_files AS old
    USING processing_status AS claimed
    WHERE claimed.attempt_id = ANY(%s::uuid[])
      AND claimed.status = 'processing'
      AND old.upload_id = claimed.upload_id
      AND old.attempt_id <> claimed.attempt_id
    """,
    """
    DELETE FROM processed_topics AS old
    USING processing_status AS claimed
    WHERE claimed.attempt_id = ANY(%s::uuid[])
      AND claimed.status = 'processing'
      AND old.upload_id = claimed.upload_id
      AND old.attempt_id <> claimed.attempt_id
    """,
)


def prune_processing_claims(cur, attempt_ids) -> None:
    """Remove predecessor registries after claiming, before transaction commit.

    The claim UPDATE keeps every selected processing_status row locked across
    these statements. An older publisher that committed while the claim waited
    is visible to the first fresh snapshot here; a publisher that has not yet
    acquired the lock cannot pass its ownership check until after this
    transaction commits, when it observes the replacement token and fails.
    """

    attempts = [str(attempt_id) for attempt_id in attempt_ids]
    if not attempts:
        return
    for sql in PROCESSING_CLAIM_PRUNE_SQL:
        cur.execute(sql, (attempts,))


class ProcessingClaimLostError(RuntimeError):
    """The upload lease expired or was fenced by a newer processor attempt."""


def canonical_attempt_id(attempt_id: str) -> str:
    """Return the canonical UUID used in generation-owned object keys."""

    try:
        return str(uuid.UUID(str(attempt_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("processing attempt_id must be a UUID") from exc


def lock_processing_claim(
    cur, upload_id: str, attempt_id: str, *, unexpired: bool = True
) -> int:
    """Lock and validate the exact processor generation in this transaction.

    ``FOR UPDATE`` serializes a registry write with lease renewal and reclaim.
    Under Postgres READ COMMITTED, a waiter re-checks the predicate against the
    new row version, so a reclaim that wins the lock fences the old publisher.
    """

    expiry_clause = "AND lease_expires_at > NOW()" if unexpired else ""
    cur.execute(
        f"""
        SELECT attempts
        FROM processing_status
        WHERE upload_id = (
            SELECT id FROM uploads WHERE client_upload_id=%s LIMIT 1
        )
          AND status = 'processing'
          AND attempt_id = %s::uuid
          {expiry_clause}
        FOR UPDATE
        """,
        (upload_id, attempt_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ProcessingClaimLostError(
            f"processing claim for upload {upload_id} is no longer owned"
        )
    return int(row[0])
