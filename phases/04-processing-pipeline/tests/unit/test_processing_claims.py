"""Fencing/lease regressions for multi-replica processor ownership."""

from pathlib import Path
from unittest.mock import Mock, patch
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor import core  # noqa: E402
from processor import pro_stages  # noqa: E402
from processor.core import (  # noqa: E402
    ProcessingClaimLostError,
    ProcessingStageError,
    ProcessorConfig,
)


class FakeCursor:
    def __init__(self, *, rows=(), fetchones=()):
        self.rows = list(rows)
        self.fetchones = list(fetchones)
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.fetchones.pop(0) if self.fetchones else None


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def config(tmp_path):
    return ProcessorConfig(parquet_root=tmp_path / "parquet", topics=[])


@pytest.mark.parametrize("flag", pro_stages.PRO_STAGE_FLAGS)
def test_core_refuses_pro_stage_flags_before_touching_any_candidate(
    monkeypatch, tmp_path, flag
):
    """Edition core has no episode, media or QA stage. A deployment that asks
    for one must fail before any claim, not silently skip the work."""
    monkeypatch.setattr(core, "check_cdr_decoder_available", lambda: None)
    monkeypatch.setenv("ROBOT_VAULT_EDITION", "core")
    monkeypatch.setenv(flag, "true")

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("no candidate may be claimed with a Pro stage flag in core")

    monkeypatch.setattr(core, "list_unprocessed_uploads", _must_not_run)
    monkeypatch.setattr(core, "_process_candidates", _must_not_run)

    with patch("processor.pro_stages.load_pro") as load_pro:
        with pytest.raises(ValueError, match=f"{flag}=true is incompatible with ROBOT_VAULT_EDITION=core"):
            core.run_once(config(tmp_path))
    load_pro.assert_not_called()


def test_claim_and_fresh_snapshot_prune_share_one_transaction(monkeypatch):
    cursor = FakeCursor(
        rows=[("u-1", "robot", "s3://bucket/u.mcap", "attempt-1", 2)]
    )
    conn = FakeConnection(cursor)
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core.psycopg, "connect", lambda _dsn: conn)

    rows = core.list_unprocessed_uploads(limit=1)

    assert rows == [("u-1", "robot", "s3://bucket/u.mcap", "attempt-1", 2)]
    assert len(cursor.statements) == 5
    sql, params = cursor.statements[0]
    assert "FOR UPDATE OF u" not in sql
    # Pin the connective too: a mutation of AND->OR here would let unverified
    # uploads back into the claim pool via SQL precedence, and a bare substring
    # check on "u.integrity_verified IS TRUE" would not catch it.
    assert "AND u.integrity_verified IS TRUE" in sql
    assert "ON CONFLICT (upload_id) DO UPDATE" in sql
    assert "processing_status.lease_expires_at" in sql
    assert "INSERT INTO processing_status" in sql
    assert "ON CONFLICT (upload_id) DO UPDATE" in sql
    assert "attempt_id = gen_random_uuid()" in sql
    assert "lease_expires_at" in sql
    prune_sql = " ".join(statement for statement, _ in cursor.statements[1:])
    assert "DELETE FROM qa_reports AS old" in prune_sql
    assert "DELETE FROM episodes AS old" in prune_sql
    assert "DELETE FROM parquet_files AS old" in prune_sql
    assert "DELETE FROM processed_topics AS old" in prune_sql
    assert "old.attempt_id <> claimed.attempt_id" in prune_sql
    assert all(
        prune_params == (["attempt-1"],)
        for _, prune_params in cursor.statements[1:]
    )
    assert params[1] == []
    assert params[2] == 1
    assert conn.commits == 1


def test_stale_owner_cannot_complete_or_insert_processed(monkeypatch):
    cursor = FakeCursor(fetchones=[None])
    conn = FakeConnection(cursor)
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core.psycopg, "connect", lambda _dsn: conn)

    with pytest.raises(ProcessingClaimLostError):
        core._pg_complete_claim(
            "u-1",
            "00000000-0000-0000-0000-000000000001",
            status="completed",
            rows_written=10,
            metadata={},
        )

    assert not any("INSERT INTO processed " in sql for sql, _ in cursor.statements)
    assert conn.commits == 0


def test_stale_owner_cannot_register_parquet_after_lease_handoff(monkeypatch):
    cursor = FakeCursor(fetchones=[None])
    conn = FakeConnection(cursor)
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core.psycopg, "connect", lambda _dsn: conn)

    with pytest.raises(ProcessingClaimLostError):
        core._pg_register_parquet_file(
            upload_id="u-1",
            robot_id="robot",
            message_type="imu",
            kind="topic",
            file_path="bucket/processed/parquet/stale.parquet",
            row_count=1,
            file_size_bytes=10,
            ts_min_ns=1,
            ts_max_ns=2,
            all_null_cols=[],
            sha256="a" * 64,
            attempt_id="00000000-0000-0000-0000-000000000001",
        )

    assert not any("INSERT INTO parquet_files" in sql for sql, _ in cursor.statements)
    assert conn.commits == 0


def test_stale_owner_cannot_delete_newer_attempt_outputs(monkeypatch):
    cursor = FakeCursor(fetchones=[None])
    conn = FakeConnection(cursor)
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core.psycopg, "connect", lambda _dsn: conn)

    with pytest.raises(ProcessingClaimLostError):
        core._pg_record_failure(
            "u-1",
            "late failure",
            attempt_id="00000000-0000-0000-0000-000000000001",
        )

    assert not any(sql.startswith("DELETE FROM") for sql, _ in cursor.statements)
    assert conn.commits == 0


def test_owned_failure_removes_partial_registry_and_dead_letters_at_limit(monkeypatch):
    cursor = FakeCursor(fetchones=[(core.PROCESSOR_MAX_ATTEMPTS,), (core.PROCESSOR_MAX_ATTEMPTS,)])
    conn = FakeConnection(cursor)
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core.psycopg, "connect", lambda _dsn: conn)

    attempts = core._pg_record_failure(
        "u-1",
        "media incomplete",
        metadata={"stages": {"media": {"status": "terminal_failure"}}},
        attempt_id="00000000-0000-0000-0000-000000000001",
    )

    assert attempts == core.PROCESSOR_MAX_ATTEMPTS
    sqls = [sql for sql, _ in cursor.statements]
    assert any("DELETE FROM qa_reports" in sql for sql in sqls)
    assert any("DELETE FROM episodes" in sql for sql in sqls)
    assert any("DELETE FROM parquet_files" in sql for sql in sqls)
    update = next(
        (sql, params)
        for sql, params in cursor.statements
        if sql.startswith("UPDATE processing_status SET status = %s")
    )
    assert update[1][0] == "dead_letter"
    assert conn.commits == 1


def test_run_once_uses_fenced_completion_for_real_claim(tmp_path, monkeypatch):
    claim = (
        "u-1",
        "robot",
        "/fake/u.mcap",
        "00000000-0000-0000-0000-000000000001",
        1,
    )
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    list_mock = Mock(side_effect=[[claim], []])
    monkeypatch.setattr(core, "list_unprocessed_uploads", list_mock)
    monkeypatch.setattr(core, "_pg_prepare_claim", Mock())
    monkeypatch.setattr(core, "_pg_renew_claim", Mock(return_value=True))
    monkeypatch.setattr(core, "_extract_minimal_topics_to_parquet", Mock(return_value=7))
    complete = Mock()
    monkeypatch.setattr(core, "_pg_complete_claim", complete)

    assert core.run_once(config(tmp_path)) == 1
    complete.assert_called_once()
    assert complete.call_args.args[:2] == ("u-1", claim[3])
    assert complete.call_args.kwargs["rows_written"] == 7
    assert list_mock.call_args_list[0].kwargs == {
        "limit": 1,
        "exclude_upload_ids": [],
    }
    assert list_mock.call_args_list[1].kwargs["exclude_upload_ids"] == ["u-1"]


@pytest.mark.parametrize(
    "attempt,status",
    [(1, "retryable_failure"), (core.PROCESSOR_MAX_ATTEMPTS, "terminal_failure")],
)
def test_stage_failure_is_never_acknowledged_success(
    tmp_path, monkeypatch, attempt, status
):
    claim = (
        "u-1",
        "robot",
        "/fake/u.mcap",
        "00000000-0000-0000-0000-000000000001",
        attempt,
    )
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core, "list_unprocessed_uploads", Mock(side_effect=[[claim], []]))
    monkeypatch.setattr(core, "_pg_prepare_claim", Mock())

    def fail_stage(*_args, stats, **_kwargs):
        core._set_stage_status(
            stats, "media", "retryable_failure", detail="one chunk failed"
        )
        raise ProcessingStageError("media", "one chunk failed")

    monkeypatch.setattr(core, "_extract_minimal_topics_to_parquet", fail_stage)
    complete = Mock()
    failed = Mock(return_value=attempt)
    monkeypatch.setattr(core, "_pg_complete_claim", complete)
    monkeypatch.setattr(core, "_pg_record_failure", failed)

    assert core.run_once(config(tmp_path)) == 0
    complete.assert_not_called()
    failed.assert_called_once()
    metadata = failed.call_args.kwargs["metadata"]
    assert metadata["stages"]["media"]["status"] == status
    assert failed.call_args.kwargs["attempt_id"] == claim[3]


def test_gc_keeps_live_or_published_generations_and_ages_out_stale_orphans():
    gc_source = (
        Path(__file__).parents[4] / "tools/s3_gc.py"
    ).read_text(encoding="utf-8")
    for table in (
        "processed_topics AS output",
        "parquet_files AS output",
        "media_files AS output",
        "qa_reports AS output",
    ):
        assert table in gc_source
    assert "output.attempt_id = published.attempt_id" in gc_source
    assert "output.attempt_id = live.attempt_id" in gc_source
    assert "live.status = 'processing'" in gc_source
    assert "live.lease_expires_at > NOW()" in gc_source
    # Reachability dropping is not immediate deletion: the existing GC age
    # floor and dry-run/delete path remain the only object remover.
    assert 'GC_MIN_AGE_HOURS' in gc_source
    assert 'default 24' in gc_source


# SQL in these modules uses uppercase FROM; keeping the scan case-sensitive
# avoids treating human-facing prose such as "rolled up from media_files"
# as a database reader. Pro reuses these two functions for its own source
# trees, so a table added here is checked there too.
PUBLISHED_TABLES = "processed_topics|parquet_files|episodes|media_files|qa_reports"
READ_PATTERN = re.compile(rf"\bFROM\s+(?:{PUBLISHED_TABLES})\b")


def discover_customer_readers(root, trees):
    """Repo-relative paths of the modules under `trees` that read a published table."""
    return {
        path.relative_to(root).as_posix()
        for tree in trees
        for path in tree.rglob("*.py")
        if READ_PATTERN.search(path.read_text(encoding="utf-8"))
    }


def assert_published_attempt_clauses(root, contracts):
    """Each reader joins on the published attempt and never admits a NULL attempt."""
    for relative_path, required in contracts.items():
        source = " ".join((root / relative_path).read_text(encoding="utf-8").split())
        for clause in required:
            assert clause in source, f"{relative_path} lacks authority clause: {clause}"
        assert "attempt_id IS NULL" not in source, (
            f"{relative_path} still admits rows without a processing attempt"
        )


def test_every_customer_reader_uses_the_published_attempt_pointer():
    """Keep a new direct reader from bypassing the publication pointer.

    The live lease-crossing test proves the predicate against deliberately
    injected stale rows. This source inventory pins that every customer-facing
    SQL reader actually applies it.
    """

    root = Path(__file__).parents[4]
    query_root = root / "phases/05-query-service/src/query_service"
    assert discover_customer_readers(root, (query_root,)) == {
        "phases/05-query-service/src/query_service/registry.py",
        "phases/05-query-service/src/query_service/uploads.py",
        "phases/05-query-service/src/query_service/routers/fleet.py",
        "phases/05-query-service/src/query_service/routers/obs.py",
    }
    assert_published_attempt_clauses(root, {
        "phases/05-query-service/src/query_service/registry.py": (
            "file.attempt_id = published.attempt_id",
        ),
        "phases/05-query-service/src/query_service/uploads.py": (
            "published.attempt_id = pt.attempt_id",
            "pf.attempt_id = pt.attempt_id",
        ),
        "phases/05-query-service/src/query_service/routers/fleet.py": (
            "pt.attempt_id = published.attempt_id",
        ),
        "phases/05-query-service/src/query_service/routers/obs.py": (
            "e.attempt_id = published.attempt_id",
            "q.attempt_id = published.attempt_id",
        ),
    })
