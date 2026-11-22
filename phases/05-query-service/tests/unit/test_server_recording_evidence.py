"""Expose verification and actual processing rows without guessing from topics."""
from datetime import datetime, timezone

import pytest

from query_service import uploads


class Cursor:
    def __init__(self, evidence):
        self.evidence = evidence
        self.query = ""
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        self.query = query
        self.queries.append((query, params))

    def fetchone(self):
        if "COUNT(*)" in self.query:
            return (1,)
        if "WHERE id::text" in self.query:
            return ("robot-1", "capture.mcap", "s3://vault/capture", 12, "completed", 1, 12,
                    None, None, None, None, None, "uuid-1")
        return None

    def fetchall(self):
        if "LEFT JOIN processing_status" in self.query:
            return self.evidence
        if "as upload_id" in self.query:
            return [("uuid-1", "robot-1", "capture.mcap", "s3://vault/capture", 12,
                     "completed", 1, 12, None, None, None, None, None)]
        return []


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self._cursor


@pytest.mark.parametrize("verified,basis,processing,error", [
    (False, "unverified", None, None),
    (True, "client_sha256", "processing", None),
    (True, "client_sha256", "dead_letter", "unsupported schema"),
    (True, "client_sha256", "completed_empty", None),
    (True, "client_sha256", "failed", "worker stopped"),
])
def test_list_and_detail_report_registered_evidence(monkeypatch, verified, basis, processing, error):
    stamp = datetime(2026, 9, 5, tzinfo=timezone.utc)
    cursor = Cursor([("uuid-1", verified, basis, None, stamp,
                      processing, error, 2 if processing else None,
                      stamp if processing else None, None, stamp, stamp)])
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(uploads.psycopg, "connect", lambda *args: Connection(cursor))
    monkeypatch.setattr(uploads, "_detail_cache", uploads.TTLCache())
    rows, total = uploads.read_index_page(None, None, None, 50, 0, "completed_at", "desc")
    assert total == 1
    evidence = rows[0].recording_evidence
    assert evidence["integrity_verified"] is verified
    assert evidence["integrity_verification_basis"] == basis
    assert evidence["processing_status"] == processing
    assert evidence["processing_error"] == error
    assert evidence["upload_updated_at"] == "2026-09-05T00:00:00Z"
    assert len([q for q, _ in cursor.queries if "LEFT JOIN processing_status" in q]) == 1
    detail = uploads.get_upload_detail("client-1")
    assert detail["status"] == "completed"
    assert detail["processing_status"] == processing
    assert detail["processing_error"] == error
    assert detail["integrity_verification_basis"] == basis
    assert detail["topics"] == []
    evidence_queries = [(q, params) for q, params in cursor.queries if "LEFT JOIN processing_status" in q]
    assert evidence_queries[-1][1] == (["uuid-1"],)
    assert "client_upload_id" not in evidence_queries[-1][0]


def test_absent_evidence_stays_unknown(monkeypatch):
    cursor = Cursor([])
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(uploads.psycopg, "connect", lambda *args: Connection(cursor))
    monkeypatch.setattr(uploads, "_detail_cache", uploads.TTLCache())
    detail = uploads.get_upload_detail("client-1")
    assert detail["integrity_verified"] is None
    assert detail["processing_status"] is None


def test_empty_page_does_not_query_recording_evidence():
    cursor = Cursor([])
    assert uploads.read_recording_evidence(cursor, []) == {}
    assert cursor.queries == []


def test_evidence_timestamps_normalize_database_session_timezone():
    from datetime import timedelta
    stamp = datetime(2026, 9, 5, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    cursor = Cursor([("uuid-1", True, "client_sha256", None, stamp,
                      "processing", None, 1, stamp, None, stamp, stamp)])
    evidence = uploads.read_recording_evidence(cursor, ["uuid-1"])["uuid-1"]
    assert evidence["upload_updated_at"] == "2026-09-05T00:00:00Z"
    assert evidence["processing_started_at"] == "2026-09-05T00:00:00Z"


def test_cached_detail_refreshes_status_without_repeating_metadata_reads(monkeypatch):
    stamp = datetime(2026, 9, 5, tzinfo=timezone.utc)
    cursor = Cursor([("uuid-1", True, "client_sha256", None, stamp,
                      "processing", None, 1, stamp, None, stamp, stamp)])
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(uploads.psycopg, "connect", lambda *args: Connection(cursor))
    monkeypatch.setattr(uploads, "_detail_cache", uploads.TTLCache())
    first = uploads.get_upload_detail("client-1")
    assert first["processing_status"] == "processing"
    metadata_queries = [q for q, _ in cursor.queries if "FROM processed" in q]
    assert metadata_queries

    cursor.evidence = [("uuid-1", True, "client_sha256", None, stamp,
                        "failed", "worker stopped", 2, stamp, None, stamp, None)]
    second = uploads.get_upload_detail("client-1")
    assert second["processing_status"] == "failed"
    assert second["processing_error"] == "worker stopped"
    assert second["processing_attempts"] == 2
    assert second["processing_lease_expires_at"] is None
    assert first["processing_status"] == "processing"
    assert second["topics"] is first["topics"]
    assert [q for q, _ in cursor.queries if "FROM processed" in q] == metadata_queries
    assert len([q for q, _ in cursor.queries if "WHERE id::text" in q]) == 2
    assert len([q for q, _ in cursor.queries if "LEFT JOIN processing_status" in q]) == 2

    monkeypatch.setattr(cursor, "fetchone", lambda: None)
    assert uploads.get_upload_detail("client-1") is None
