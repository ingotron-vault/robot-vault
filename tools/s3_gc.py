#!/usr/bin/env python3
"""S3 GC: deletes S3 objects that are unreachable from Postgres records.

Safety contract (see docs/CONFIG_REFERENCE.md and the GC reachability tests):

Reachability is computed by normalizing every DB-stored path down to a bare
S3 object key (no `s3://` scheme, no leading `<bucket>/` segment) and
comparing that against listed object keys, which boto3 already returns bare.
A query that compared against `storage_path LIKE 's3://%'` /
`parquet_path LIKE 's3://%'` would match zero rows, because the processor
writes `parquet_path` WITHOUT an `s3://` prefix (see
phases/04-processing-pipeline/src/processor/core.py `_s3_parquet_root()` ->
f"{bucket}/processed/parquet"); every processed Parquet object would then look
unreachable and be deleted. Normalizing both sides prevents that.

The processor also registers every Parquet file it writes in
`parquet_files`, which this script reads as an additional (never subtractive)
reachability source -- that is what keeps processed objects reachable
independently of any path template, including if a future schema version
writes under a different prefix. No object is derived from a path template.

Objects of an attempt that is still running are protected by prefix. The
processor uploads media chunks, QA blobs and Parquet files one at a time and
registers them only when the stage ends. Every key with a path segment
`_attempts/{attempt_id}` whose attempt holds an unexpired lease in
`processing_status` is therefore treated as reachable. The sweep also judges
object age at the time the snapshot was taken, so an object written after the
snapshot (for example by a claim that started later) is never a candidate.

Safety invariant: the collector tracks both the reachable keys and whether
the database query completely covers each S3 namespace. A successfully
queried, empty registry is a *known-empty* namespace, so aged orphan objects
there can be swept. A missing registry, failed query, custom/unmodelled
namespace, or other unknown coverage state is never interpreted as "nothing
is live" and is skipped. In particular, `cas/files/` remains fail-closed:
some upload rows happen to point at CAS keys, but this collector does not yet
model the CAS reference lifecycle completely enough to declare that namespace
covered.

Usage / env vars (see configs/gc.yaml for the YAML equivalents; env vars
always win):
  DATABASE_URL        Postgres DSN (required).
  S3_BUCKET            Target bucket (required).
  S3_ENDPOINT, S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
                       S3 connection (path style, SigV4).
  GC_DRY_RUN           "true"/"false" (default in docker-compose.yml is
                       "true"). When true, candidates are logged
                       ("DRY-DELETE <key>") but nothing is deleted -- use
                       this on every first deploy / config change before
                       flipping it to "false". Any other spelling is a fatal
                       configuration error; it never falls through to delete.
  GC_MIN_AGE_HOURS     Minimum object age before it's eligible for deletion
                       (default 24; finite 1 second through 87,600 hours).
  GC_MPU_MAX_AGE_HOURS Incomplete (never-completed) S3 multipart uploads older
                       than this are aborted to reclaim their orphaned parts
                       (default 24). Registered resumable sessions are excluded;
                       ingest owns their expiry under the upload lock.
                       Parts of an in-progress MPU are invisible
                       to list_objects_v2, so the object sweep above can never
                       see them; this dedicated sweep uses list_multipart_uploads
                       + abort_multipart_upload. Honors GC_DRY_RUN (dry-run logs
                       "DRY-ABORT-MPU ..." but aborts nothing).
  GC_DELETE_LIMIT      Max objects deleted per run, shared across all
                       prefixes (default 5000; range 1..100,000).
  GC_PREFIXES          Comma-separated S3 prefixes to sweep (default
                       "raw/,manifests/,processed/parquet/,cas/files/,
                       processed/media/,qa/reports/,exports/").
                       Note: "cas/files/" is intentionally uncovered and is
                       always skipped by the object sweep. `raw/`, `manifests/`
                       and `processed/parquet/` are covered by the core schema.
                       `processed/media/`, `qa/reports/`, and `exports/` become
                       covered when their respective registry tables exist and
                       can be queried. Once covered, an empty registry means
                       the namespace is known empty and aged orphans are
                       eligible for the normal dry-run/delete flow.
  GC_INTERVAL_SECONDS  Seconds between GC passes (default 3600; range
                       1..604,800).
  METRICS_PORT         Prometheus metrics HTTP port (default 9108).
"""
import os
import math
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Set, Tuple, List, Optional

import psycopg
import boto3
from botocore.config import Config as BotoConfig
from prometheus_client import Counter, Gauge, Summary, start_http_server
import yaml


# ---------------------------------------------------------------------------
# Prometheus metrics -- MODULE LEVEL.
#
# They must not be constructed inside run_gc(), which is called once per
# poll-loop iteration in main(). prometheus_client's default registry raises
# `ValueError: Duplicated timeseries` if a metric with the same name is
# registered twice, so the second call to run_gc() would crash the
# process. Metrics must be created exactly once per process, so they live at
# module level: run_gc() is re-entrant across iterations.
# ---------------------------------------------------------------------------
GC_RUN_SECONDS = Summary('gc_run_seconds', 'Duration of GC run in seconds')
GC_SCANNED_OBJECTS = Gauge('gc_scanned_objects', 'Number of S3 objects scanned in last run')
GC_DELETED_OBJECTS = Gauge('gc_deleted_objects', 'Number of S3 objects deleted in last run')
GC_CANDIDATE_OBJECTS = Gauge('gc_candidate_objects', 'Number of candidate objects in last run (pre-delete)')
GC_RUNS_TOTAL = Counter('gc_runs_total', 'Total number of GC runs')
GC_RUNS_SUCCESS_TOTAL = Counter('gc_runs_success_total', 'Total number of successful GC runs')
GC_RUNS_FAILURE_TOTAL = Counter('gc_runs_failure_total', 'Total number of failed GC runs')
GC_SKIPPED_EMPTY_REACHABLE_PREFIXES = Counter(
    'gc_skipped_empty_reachable_prefixes_total',
    'Number of prefix sweeps skipped because reachability coverage was unknown',
)
GC_ABORTED_MPUS = Gauge('gc_aborted_mpus', 'Number of incomplete multipart uploads aborted in last run')
GC_CANDIDATE_MPUS = Gauge('gc_candidate_mpus', 'Number of incomplete-MPU abort candidates in last run (pre-abort)')


def normalize_object_key(raw_path: Optional[str], bucket: str) -> str:
    """Normalize a DB-stored path to a bare S3 object key (no scheme, no bucket).

    Handles every format actually written to Postgres today:
      - "s3://bucket/key"  (uploads.storage_path)                      -> "key"
      - "bucket/key"       (processed_topics.parquet_path, no scheme)  -> "key"
      - "key"              (already bare)                              -> "key" (unchanged)

    Listed S3 object keys from boto3's list_objects_v2 are already bare
    (Bucket is a separate parameter), so this function only needs to be
    applied to the DB side for both sides to compare equal.
    """
    if not raw_path:
        return ''
    s = raw_path
    if s.startswith('s3://'):
        s = s[len('s3://'):]
    bucket_prefix = f"{bucket}/"
    if s.startswith(bucket_prefix):
        s = s[len(bucket_prefix):]
    return s


class ReachabilitySnapshot(set):
    """Set-compatible reachability result with namespace coverage metadata.

    A plain set cannot distinguish "the registry query succeeded and returned
    no rows" from "we have no derivation for this namespace". `covered_prefixes`
    carries that distinction while subclassing `set` keeps existing callers
    (`key in reachable`, iteration, equality, and set operations) working.

    Converting a snapshot back to a plain `set` deliberately drops coverage;
    a plain set covers no namespace, so the sweep skips every prefix.

    `protected_attempts` holds the attempt ids of unexpired processing leases
    (see `key_in_protected_attempt`). `taken_at` is the wall-clock time before
    the first query ran; the sweep measures object age at that time.
    """

    def __init__(
        self,
        values=(),
        *,
        covered_prefixes=(),
        protected_mpus=(),
        protected_attempts=(),
        taken_at: Optional[float] = None,
    ):
        super().__init__(values)
        self.covered_prefixes = frozenset(covered_prefixes)
        self.protected_mpus = frozenset(protected_mpus)
        self.protected_attempts = frozenset(protected_attempts)
        self.taken_at = taken_at


def key_in_protected_attempt(key: str, protected_attempts) -> bool:
    """True if a path segment `_attempts` is followed by a protected id.

    Whole segments only: `_attempts/<id>x/` does not match `<id>`. Every
    occurrence is checked, so a robot id that is literally `_attempts` cannot
    hide the real attempt segment.
    """
    if not protected_attempts:
        return False
    parts = key.split("/")
    return any(
        parts[index] == "_attempts" and parts[index + 1] in protected_attempts
        for index in range(len(parts) - 1)
    )


def reachable_keys_from_pg(dsn: str, bucket: str) -> ReachabilitySnapshot:
    """Build the set of bare S3 object keys that must NOT be garbage collected.

    Seven sources, covering everything the pipeline writes to S3, plus the
    live-attempt prefixes described in the module docstring:
      1. uploads.storage_path            -- raw MCAP objects (raw/mcap/...)
      2. manifests.manifest_s3_path      -- ingest audit/CAS manifest objects
      3. processed_topics.parquet_path   -- per-topic processed Parquet
      4. parquet_files.file_path         -- the Parquet registry, which the
         processor writes for every Parquet file it produces and which is
         layout-agnostic: a future schema version writing under a different
         prefix stays reachable without touching this file.
      5. media_files.key                 -- the media registry (Pro) for
         encoded video chunks under `processed/media/`.
      6. qa_reports.detail_key           -- the QA registry (Pro) for QA
         report blobs under `qa/reports/`.
      7. export_files.key                -- the export registry (Pro) for
         exported dataset files under `exports/`.

    Sources (4)-(7) and the multipart-session table are each probed with
    `to_regclass` before they are read, so this script still works against a
    database where that table is absent. All of them live in
    `db/migrations/001_baseline.sql`.

    Sources are additive by construction: no source can shrink the reachable
    set, so adding (4), (5), (6), or (7) cannot make a previously-safe object
    collectable. The returned object is set-compatible and also records which
    namespaces were completely queried:

      - `raw/`, `manifests/`, and `processed/parquet/` after all core-schema
        queries succeed;
      - optional media/QA/export namespaces only when their registry table
        exists and its SELECT succeeds;
      - never `cas/files/`, whose full reference lifecycle is not modelled.

    Any database error is propagated. The caller therefore receives no
    snapshot at all and cannot turn a failed query into an empty live set.
    """
    keys: Set[str] = set()
    covered_prefixes: Set[str] = set()
    protected_mpus: Set[Tuple[str, str]] = set()
    protected_attempts: Set[str] = set()
    # Taken BEFORE any query: an object written after this moment may belong
    # to a claim or registration the queries below did not see.
    taken_at = time.time()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Live leases first. A lease that is live now protects its whole
            # `_attempts/{attempt_id}/` prefix for this pass, including
            # objects it registers or uploads while the queries below run.
            cur.execute(
                "SELECT attempt_id::text FROM processing_status "
                "WHERE status = 'processing' AND attempt_id IS NOT NULL "
                "AND lease_expires_at > NOW()"
            )
            for (attempt_id,) in cur.fetchall():
                if attempt_id:
                    protected_attempts.add(str(attempt_id))

            cur.execute(
                "SELECT storage_path FROM uploads WHERE storage_path IS NOT NULL AND storage_path <> ''"
            )
            for (raw,) in cur.fetchall():
                key = normalize_object_key(raw, bucket)
                if key:
                    keys.add(key)

            cur.execute(
                "SELECT manifest_s3_path FROM manifests "
                "WHERE manifest_s3_path IS NOT NULL AND manifest_s3_path <> ''"
            )
            for (raw,) in cur.fetchall():
                key = normalize_object_key(raw, bucket)
                if key:
                    keys.add(key)

            cur.execute(
                """
                SELECT parquet_path
                FROM processed_topics AS output
                LEFT JOIN processed AS published
                  ON published.upload_id = output.upload_id
                LEFT JOIN processing_status AS live
                  ON live.upload_id = output.upload_id
                WHERE parquet_path IS NOT NULL AND parquet_path <> ''
                  AND (
                    output.attempt_id = published.attempt_id
                    OR (
                      output.attempt_id = live.attempt_id
                      AND live.status = 'processing'
                      AND live.lease_expires_at > NOW()
                    )
                  )
                """
            )
            for (raw,) in cur.fetchall():
                key = normalize_object_key(raw, bucket)
                if key:
                    keys.add(key)

            # `to_regclass` rather than catching UndefinedTable: an aborted
            # transaction would poison the remaining queries in this
            # connection.
            cur.execute("SELECT to_regclass('public.parquet_files')")
            row = cur.fetchone()
            if row and row[0] is not None:
                cur.execute(
                    """
                    SELECT file_path
                    FROM parquet_files AS output
                    LEFT JOIN processed AS published
                      ON published.upload_id = output.upload_id
                    LEFT JOIN processing_status AS live
                      ON live.upload_id = output.upload_id
                    WHERE file_path IS NOT NULL AND file_path <> ''
                      AND (
                        output.attempt_id = published.attempt_id
                        OR (
                          output.attempt_id = live.attempt_id
                          AND live.status = 'processing'
                          AND live.lease_expires_at > NOW()
                        )
                      )
                    """
                )
                for (raw,) in cur.fetchall():
                    key = normalize_object_key(raw, bucket)
                    if key:
                        keys.add(key)

            # `media_files`, the encoded-video registry. Same
            # `to_regclass` existence guard as `parquet_files` above.
            cur.execute("SELECT to_regclass('public.media_files')")
            row = cur.fetchone()
            if row and row[0] is not None:
                cur.execute(
                    """
                    SELECT key
                    FROM media_files AS output
                    JOIN episodes AS episode
                      ON episode.episode_id = output.episode_id
                    LEFT JOIN processed AS published
                      ON published.upload_id = episode.upload_id
                    LEFT JOIN processing_status AS live
                      ON live.upload_id = episode.upload_id
                    WHERE key IS NOT NULL AND key <> ''
                      AND (
                        output.attempt_id = published.attempt_id
                        OR (
                          output.attempt_id = live.attempt_id
                          AND live.status = 'processing'
                          AND live.lease_expires_at > NOW()
                        )
                      )
                      AND (
                        episode.attempt_id = published.attempt_id
                        OR (
                          episode.attempt_id = live.attempt_id
                          AND live.status = 'processing'
                          AND live.lease_expires_at > NOW()
                        )
                      )
                    """
                )
                for (raw,) in cur.fetchall():
                    key = normalize_object_key(raw, bucket)
                    if key:
                        keys.add(key)
                covered_prefixes.add('processed/media/')

            # `qa_reports`, the QA-report-blob registry. Same
            # `to_regclass` existence guard as `parquet_files` above.
            cur.execute("SELECT to_regclass('public.qa_reports')")
            row = cur.fetchone()
            if row and row[0] is not None:
                cur.execute(
                    """
                    SELECT detail_key
                    FROM qa_reports AS output
                    LEFT JOIN episodes AS episode
                      ON episode.episode_id = output.episode_id
                    LEFT JOIN processed AS published
                      ON published.upload_id = COALESCE(
                           output.upload_id, episode.upload_id
                         )
                    LEFT JOIN processing_status AS live
                      ON live.upload_id = COALESCE(
                           output.upload_id, episode.upload_id
                         )
                    WHERE detail_key IS NOT NULL AND detail_key <> ''
                      AND (
                        output.attempt_id = published.attempt_id
                        OR (
                          output.attempt_id = live.attempt_id
                          AND live.status = 'processing'
                          AND live.lease_expires_at > NOW()
                        )
                      )
                      AND (
                        episode.episode_id IS NULL
                        OR episode.attempt_id = published.attempt_id
                        OR (
                          episode.attempt_id = live.attempt_id
                          AND live.status = 'processing'
                          AND live.lease_expires_at > NOW()
                        )
                      )
                    """
                )
                for (raw,) in cur.fetchall():
                    key = normalize_object_key(raw, bucket)
                    if key:
                        keys.add(key)
                covered_prefixes.add('qa/reports/')

            # `export_files`, the exported-dataset-file registry.
            # Same `to_regclass` existence guard as `parquet_files` above.
            cur.execute("SELECT to_regclass('public.export_files')")
            row = cur.fetchone()
            if row and row[0] is not None:
                cur.execute(
                    "SELECT key FROM export_files WHERE key IS NOT NULL AND key <> ''"
                )
                for (raw,) in cur.fetchall():
                    key = normalize_object_key(raw, bucket)
                    if key:
                        keys.add(key)
                covered_prefixes.add('exports/')

            # These core namespaces are known only after *all* core queries
            # succeeded. An exception above propagates without returning a
            # partial snapshot.
            covered_prefixes.update(('raw/', 'manifests/', 'processed/parquet/'))

            # Ingest expires durable sessions under the upload advisory lock.
            # An expired row still protects its MPU here until ingest removes
            # it. Query failures propagate instead of authorizing deletion.
            cur.execute("SELECT to_regclass('public.upload_multipart_sessions')")
            row = cur.fetchone()
            if row and row[0] is not None:
                cur.execute(
                    "SELECT u.storage_path, s.s3_upload_id "
                    "FROM upload_multipart_sessions s JOIN uploads u ON u.id=s.upload_id"
                )
                for storage_path, multipart_id in cur.fetchall():
                    if storage_path and storage_path.startswith(f's3://{bucket}/') and multipart_id:
                        protected_mpus.add((normalize_object_key(storage_path, bucket), multipart_id))
    return ReachabilitySnapshot(
        keys,
        covered_prefixes=covered_prefixes,
        protected_mpus=protected_mpus,
        protected_attempts=protected_attempts,
        taken_at=taken_at,
    )


@dataclass
class GCConfig:
    bucket: str
    prefixes: List[str]
    min_age_seconds: int
    delete_limit: int
    dry_run: bool
    # Incomplete multipart uploads older than this are aborted to reclaim
    # orphaned parts. main() sets it from GC_MPU_MAX_AGE_HOURS (default 24h).
    mpu_max_age_seconds: int = 24 * 3600


def _config_value(env_name: str, yaml_values: dict, yaml_name: str, default):
    """Resolve one GC setting without treating false/zero as absent."""
    if env_name in os.environ:
        return os.environ[env_name]
    value = yaml_values.get(yaml_name)
    return default if value is None else value


def parse_strict_bool(name: str, value) -> bool:
    """Accept only an actual boolean or an exact true/false string.

    A misspelling must never turn the collector from dry-run into deletion.
    Case is intentionally harmless, while whitespace, numeric values and all
    other truthy/falsy spellings are configuration errors.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value == value.strip():
        normalized = value.lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError(f"{name} must be exactly true or false")


def parse_bounded_int(name: str, value, *, minimum: int, maximum: int) -> int:
    """Parse a base-10 integer and enforce an explicit operational bound."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    token = str(value)
    if not re.fullmatch(r"[0-9]+", token):
        raise ValueError(f"{name} must be an integer")
    parsed = int(token, 10)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def parse_bounded_float(
    name: str, value, *, minimum: float, maximum: float
) -> float:
    """Parse a finite numeric value and enforce an inclusive safe bound."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    token = str(value)
    if token != token.strip() or not token:
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = float(token)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def parse_prefixes(value) -> List[str]:
    """Parse explicit, canonical S3 namespace prefixes."""
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("GC_PREFIXES must contain at least one prefix")
    prefixes: List[str] = []
    for candidate in raw:
        prefix = str(candidate)
        if (
            prefix != prefix.strip()
            or not prefix
            or prefix.startswith("/")
            or not prefix.endswith("/")
            or ".." in prefix.split("/")
            or any(character in prefix for character in "*?[\\")
        ):
            raise ValueError(
                "GC_PREFIXES entries must be relative, canonical prefixes ending in /"
            )
        prefixes.append(prefix)
    if len(prefixes) != len(set(prefixes)):
        raise ValueError("GC_PREFIXES must not contain duplicate prefixes")
    return prefixes


def _list_keys(s3, bucket: str, prefix: str) -> Iterable[dict]:
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []) or []:
            yield obj


def reachable_for_prefix(reachable: Set[str], prefix: str) -> Set[str]:
    """Restrict the global reachable set to keys that fall under `prefix`."""
    return {k for k in reachable if k.startswith(prefix)}


def reachability_known_for_prefix(reachable: Set[str], prefix: str) -> bool:
    """Return whether `reachable` completely covers `prefix`.

    A snapshot covers every sub-prefix of a covered namespace, but not a
    broader parent (`processed/parquet/robot-a/` is covered when
    `processed/parquet/` is; `processed/` is not). A plain set carries no
    coverage data and covers nothing, so its prefixes are never swept.
    """
    covered_prefixes = getattr(reachable, 'covered_prefixes', None)
    if covered_prefixes is None:
        return False
    return any(prefix.startswith(covered) for covered in covered_prefixes)


def sweep_prefix(
    s3,
    bucket: str,
    prefix: str,
    reachable: Set[str],
    min_age_seconds: int,
    delete_limit: int,
    now: Optional[float] = None,
    *,
    reachability_known: bool,
    protected_attempts=frozenset(),
) -> Tuple[int, List[dict], bool]:
    """Scan one prefix and compute delete candidates.

    `reachable` must already be the reachable set restricted to this prefix
    (see `reachable_for_prefix`) -- not the full global reachable set.

    Coverage guard: if reachability for this namespace is unknown, this prefix
    is skipped entirely (no listing, no deletion) and `(0, [], True)` is
    returned. A known-empty namespace is safe to list and sweep because the
    empty result came from a successful, complete registry query, not from a
    missing derivation.

    `reachability_known` is the snapshot's explicit coverage for this prefix.
    Keys under a protected (live) attempt are never candidates. `now` should
    be the snapshot time: age is measured then, not at listing time.

    Returns (scanned_count, delete_candidates, skipped_unknown_reachability).
    """
    if not reachability_known:
        return 0, [], True

    if now is None:
        now = time.time()
    scanned = 0
    to_delete: List[dict] = []
    for obj in _list_keys(s3, bucket, prefix):
        scanned += 1
        key = obj.get('Key') or ''
        if not key:
            continue
        if key in reachable:
            continue
        if key_in_protected_attempt(key, protected_attempts):
            continue
        lm = obj.get('LastModified')
        if not lm:
            continue
        age = now - lm.timestamp()
        if age < min_age_seconds:
            continue
        to_delete.append({'Key': key})
        if len(to_delete) >= delete_limit:
            break
    return scanned, to_delete, False


def compute_delete_set(s3, cfg: GCConfig, reachable: Set[str]) -> Tuple[int, List[dict], List[str]]:
    """Run the sweep across every configured prefix.

    `cfg.delete_limit` is a global cap shared across all prefixes (by design:
    once enough candidates are found, later prefixes are skipped).

    Returns (total_scanned, delete_candidates, skipped_prefixes) where
    `skipped_prefixes` lists prefixes skipped by the unknown-coverage guard.
    """
    scanned_total = 0
    to_delete: List[dict] = []
    skipped_prefixes: List[str] = []
    protected_attempts = getattr(reachable, 'protected_attempts', frozenset())
    taken_at = getattr(reachable, 'taken_at', None)
    for pfx in cfg.prefixes:
        remaining = cfg.delete_limit - len(to_delete)
        if remaining <= 0:
            break
        pfx_reachable = reachable_for_prefix(reachable, pfx)
        pfx_reachability_known = reachability_known_for_prefix(reachable, pfx)
        scanned, candidates, skipped = sweep_prefix(
            s3,
            cfg.bucket,
            pfx,
            pfx_reachable,
            cfg.min_age_seconds,
            remaining,
            now=min(time.time(), taken_at) if taken_at is not None else None,
            reachability_known=pfx_reachability_known,
            protected_attempts=protected_attempts,
        )
        scanned_total += scanned
        to_delete.extend(candidates)
        if skipped:
            skipped_prefixes.append(pfx)
            GC_SKIPPED_EMPTY_REACHABLE_PREFIXES.inc()
            print(
                f"gc_error: reachability coverage for prefix '{pfx}' is unknown; "
                f"refusing to sweep it",
                file=sys.stderr,
            )
    return scanned_total, to_delete, skipped_prefixes


def reclaim_incomplete_mpus(
    s3,
    bucket: str,
    max_age_seconds: int,
    dry_run: bool,
    prefix: str = '',
    now: Optional[float] = None,
    protected_mpus: Optional[Set[Tuple[str, str]]] = None,
) -> Tuple[int, List[dict]]:
    """Abort incomplete S3 multipart uploads older than `max_age_seconds`.

    A crashed/interrupted streaming upload can leave an
    in-progress MPU behind: its already-uploaded parts occupy storage but are
    invisible to `list_objects_v2` (which only lists completed objects), so the
    reachability object sweep can never reclaim them. This sweep uses
    `list_multipart_uploads` + `abort_multipart_upload` to reclaim them once
    they're older than the threshold -- the programmatic equivalent of an S3
    "abort incomplete multipart upload after N days" lifecycle rule.

    Age is measured from each MPU's `Initiated` timestamp. `prefix` scopes the
    sweep so a caller can reclaim only MPUs whose key falls under a given prefix
    and never touch unrelated in-flight uploads; empty means the whole bucket.

    Scoping is done CLIENT-SIDE (`key.startswith(prefix)`), NOT via the S3
    `Prefix` request parameter: some S3-compatible servers' `ListMultipartUploads`
    match only when `Prefix` equals the full key, so passing `Prefix` there
    silently drops every real orphan. Listing
    unfiltered and filtering here is correct on every S3 server. Incomplete
    MPUs are few (only orphans + genuinely in-flight uploads), so the unfiltered
    listing stays cheap.

    `protected_mpus` contains (key, MPU ID) pairs owned by durable sessions.
    They are excluded even after expiry: ingest owns their locked cleanup.

    When `dry_run` is True, candidates are logged ("DRY-ABORT-MPU ...") but never
    aborted.

    Returns (aborted_count, candidates), where each candidate is
    `{'Key': ..., 'UploadId': ...}`. In dry-run, aborted_count is 0 but
    candidates still lists what would have been aborted.
    """
    if now is None:
        now = time.time()

    candidates: List[dict] = []
    paginator = s3.get_paginator('list_multipart_uploads')
    for page in paginator.paginate(Bucket=bucket):
        for up in page.get('Uploads', []) or []:
            key = up.get('Key')
            upload_id = up.get('UploadId')
            if not key or not upload_id:
                continue
            if protected_mpus and (key, upload_id) in protected_mpus:
                continue
            if prefix and not key.startswith(prefix):
                # Out of scope for this sweep (client-side prefix filter).
                continue
            initiated = up.get('Initiated')
            if initiated is None:
                continue
            age = now - initiated.timestamp()
            if age < max_age_seconds:
                # Too young: an upload that may still be actively streaming.
                continue
            candidates.append({'Key': key, 'UploadId': upload_id})

    if dry_run:
        for c in candidates:
            print(f"DRY-ABORT-MPU key={c['Key']} upload_id={c['UploadId']}")
        return 0, candidates

    aborted = 0
    for c in candidates:
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=c['Key'], UploadId=c['UploadId'])
            aborted += 1
        except Exception as e:
            print(
                f"gc_error: failed to abort MPU key={c['Key']} upload_id={c['UploadId']}: {e}",
                file=sys.stderr,
            )
    if aborted:
        print(f"GC aborted {aborted} incomplete multipart uploads (prefix='{prefix}')")
    return aborted, candidates


def _make_s3_client():
    return boto3.client(
        's3',
        endpoint_url=os.environ.get('S3_ENDPOINT'),
        aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
        config=BotoConfig(
            region_name=os.environ.get('S3_REGION', 'us-east-1'),
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
        ),
    )


def run_gc(cfg: GCConfig, reachable: Set[str], s3=None) -> Tuple[int, int]:
    """Execute one GC pass and update metrics.

    Safe to call repeatedly across the poll loop in main(): all Prometheus
    metrics are module-level singletons (see top of file), so no
    re-registration happens on subsequent calls.

    `s3` may be injected (e.g. by tests); when omitted a client is built
    from the standard S3_ENDPOINT/AWS_* env vars.
    """

    @GC_RUN_SECONDS.time()
    def _inner() -> Tuple[int, int, int, bool]:
        client = s3 if s3 is not None else _make_s3_client()
        scanned, to_delete, _skipped_prefixes = compute_delete_set(client, cfg, reachable)
        candidates = len(to_delete)

        # Reclaim orphaned incomplete multipart uploads left behind by
        # crashed/interrupted streaming uploads. Their parts are invisible to the
        # object sweep above (list_objects_v2 never returns MPU parts), so this is
        # the only place they get reclaimed. Scoped to the same configured
        # prefixes as the object sweep, so a run scoped to a prefix with no MPUs
        # (e.g. the GC reachability integration test) never touches unrelated in-flight
        # uploads. Honors dry_run (dry-run logs candidates but aborts nothing).
        mpu_aborted = 0
        mpu_candidates = 0
        for pfx in cfg.prefixes:
            aborted, cands = reclaim_incomplete_mpus(
                client, cfg.bucket, cfg.mpu_max_age_seconds, cfg.dry_run, prefix=pfx,
                protected_mpus=getattr(reachable, 'protected_mpus', ()),
            )
            mpu_aborted += aborted
            mpu_candidates += len(cands)
        GC_ABORTED_MPUS.set(mpu_aborted)
        GC_CANDIDATE_MPUS.set(mpu_candidates)

        if cfg.dry_run or not to_delete:
            print(f"GC dry-run or nothing to delete. scanned={scanned} candidates={candidates}")
            for obj in to_delete:
                print(f"DRY-DELETE {obj['Key']}")
            return scanned, 0, candidates, True

        # Batch delete in chunks of 1000 (S3 DeleteObjects limit).
        #
        # Quiet=False (i.e. NOT the S3 "Quiet" mode) is required here: per
        # the S3 DeleteObjects spec, Quiet mode suppresses the `Deleted`
        # array for successful deletions in the response, returning only
        # errors. With Quiet=True `resp['Deleted']`
        # is always empty on success, so `deleted` would always
        # be reported as 0 even when objects were actually removed -- the
        # gc_deleted_objects metric and this function's return value would be
        # silently wrong. Quiet=False gets back the actual per-key result so
        # deletions and errors can both be counted accurately.
        deleted = 0
        errors: List[dict] = []
        try:
            for i in range(0, len(to_delete), 1000):
                batch = to_delete[i:i + 1000]
                resp = client.delete_objects(Bucket=cfg.bucket, Delete={'Objects': batch, 'Quiet': False})
                deleted += len(resp.get('Deleted', []) or [])
                errors.extend(resp.get('Errors', []) or [])
            if errors:
                for err in errors:
                    print(
                        f"gc_error: failed to delete {err.get('Key')}: "
                        f"{err.get('Code')} {err.get('Message')}",
                        file=sys.stderr,
                    )
            print(f"GC deleted {deleted} objects (scanned={scanned})")
            return scanned, deleted, candidates, not errors
        except Exception:
            return scanned, deleted, candidates, False

    GC_RUNS_TOTAL.inc()
    scanned, deleted, candidates, ok = _inner()
    GC_SCANNED_OBJECTS.set(scanned)
    GC_DELETED_OBJECTS.set(deleted)
    GC_CANDIDATE_OBJECTS.set(candidates)
    if ok:
        GC_RUNS_SUCCESS_TOTAL.inc()
    else:
        GC_RUNS_FAILURE_TOTAL.inc()
    return scanned, deleted


def main() -> int:
    # Load YAML config if present
    cfg_path = os.environ.get('GC_CONFIG_PATH', '/app/configs/gc.yaml')
    y = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r') as f:
                y = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"warn: failed to load {cfg_path}: {e}", file=sys.stderr)

    # Resolve core settings with env override > yaml > default
    dsn = os.environ.get('DATABASE_URL') or y.get('database_url')
    bucket = os.environ.get('S3_BUCKET') or ((y.get('s3') or {}).get('bucket'))
    if not (dsn and bucket):
        print('DATABASE_URL and S3_BUCKET are required', file=sys.stderr)
        return 2

    # Fill S3/env defaults from YAML if missing
    s3y = y.get('s3') or {}
    os.environ.setdefault('S3_ENDPOINT', s3y.get('endpoint') or '')
    os.environ.setdefault('S3_REGION', s3y.get('region') or 'us-east-1')
    if s3y.get('access_key_id'):
        os.environ.setdefault('AWS_ACCESS_KEY_ID', s3y.get('access_key_id'))
    if s3y.get('secret_access_key'):
        os.environ.setdefault('AWS_SECRET_ACCESS_KEY', s3y.get('secret_access_key'))

    gy = y.get('gc') or {}
    default_prefixes = [
        'raw/',
        'manifests/',
        'processed/parquet/',
        'cas/files/',
        'processed/media/',
        'qa/reports/',
        'exports/',
    ]
    try:
        # Parse every safety-sensitive setting before starting metrics or
        # touching Postgres/S3. One second is the smallest permitted age; the
        # upper bounds catch unit mistakes while remaining well beyond normal
        # retention windows.
        port = parse_bounded_int(
            'METRICS_PORT',
            _config_value('METRICS_PORT', gy, 'metrics_port', 9108),
            minimum=1,
            maximum=65535,
        )
        prefixes = parse_prefixes(
            _config_value('GC_PREFIXES', gy, 'prefixes', default_prefixes)
        )
        min_age_hours = parse_bounded_float(
            'GC_MIN_AGE_HOURS',
            _config_value('GC_MIN_AGE_HOURS', gy, 'min_age_hours', 24),
            minimum=1 / 3600,
            maximum=87600,
        )
        mpu_max_age_hours = parse_bounded_float(
            'GC_MPU_MAX_AGE_HOURS',
            _config_value('GC_MPU_MAX_AGE_HOURS', gy, 'mpu_max_age_hours', 24),
            minimum=1 / 3600,
            maximum=87600,
        )
        delete_limit = parse_bounded_int(
            'GC_DELETE_LIMIT',
            _config_value('GC_DELETE_LIMIT', gy, 'delete_limit', 5000),
            minimum=1,
            maximum=100000,
        )
        dry_run = parse_strict_bool(
            'GC_DRY_RUN', _config_value('GC_DRY_RUN', gy, 'dry_run', True)
        )
        interval = parse_bounded_int(
            'GC_INTERVAL_SECONDS',
            _config_value('GC_INTERVAL_SECONDS', gy, 'interval_seconds', 3600),
            minimum=1,
            maximum=604800,
        )
    except ValueError as exc:
        print(f'gc_config_error: {exc}', file=sys.stderr)
        return 2

    start_http_server(port)

    cfg = GCConfig(
        bucket=bucket,
        prefixes=prefixes,
        min_age_seconds=int(min_age_hours * 3600.0),
        delete_limit=delete_limit,
        dry_run=dry_run,
        mpu_max_age_seconds=int(mpu_max_age_hours * 3600.0),
    )
    # Run forever to keep metrics HTTP server alive
    while True:
        reachable = reachable_keys_from_pg(dsn, bucket)
        try:
            run_gc(cfg, reachable)
        except Exception as e:
            print(f"gc_run_error: {e}", file=sys.stderr)
        time.sleep(interval)
    # unreachable
    # return 0


if __name__ == '__main__':
    raise SystemExit(main())
