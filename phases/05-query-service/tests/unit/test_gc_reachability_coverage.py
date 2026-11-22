"""CI-gated unit tests for GC namespace coverage versus live-key count.

The live integration suite proves real Postgres/object store behavior, but it cannot
run in the ordinary unit gate. These fakes pin the safety distinction that an
empty registry can be authoritative while an unmodelled or absent registry is
unknown and must remain fail-closed.
"""

from datetime import datetime, timezone
from pathlib import Path
import re
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tools"))
import s3_gc  # noqa: E402


class _FakeCursor:
    def __init__(self, *, existing_tables, rows=None, fail_on=None):
        self.existing_tables = set(existing_tables)
        self.rows = rows or {}
        self.fail_on = fail_on
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=None):
        self.sql = " ".join(str(sql).split())
        if self.fail_on and self.fail_on in self.sql:
            raise RuntimeError("fake database read failure")

    def fetchone(self):
        match = re.search(r"to_regclass\('public\.([^']+)'\)", self.sql)
        assert match, f"unexpected fetchone query: {self.sql}"
        table = match.group(1)
        return (f"public.{table}" if table in self.existing_tables else None,)

    def fetchall(self):
        if "FROM processing_status" in self.sql:
            return self.rows.get("live_attempts", [])
        if "FROM upload_multipart_sessions" in self.sql:
            return self.rows.get("upload_multipart_sessions", [])
        if "SELECT storage_path FROM uploads" in self.sql:
            return self.rows.get("uploads", [])
        if "SELECT manifest_s3_path FROM manifests" in self.sql:
            return self.rows.get("manifests", [])
        if "SELECT parquet_path FROM processed_topics" in self.sql:
            return self.rows.get("processed_topics", [])
        if "SELECT file_path FROM parquet_files" in self.sql:
            return self.rows.get("parquet_files", [])
        if "SELECT key FROM media_files" in self.sql:
            return self.rows.get("media_files", [])
        if "SELECT detail_key FROM qa_reports" in self.sql:
            return self.rows.get("qa_reports", [])
        if "SELECT key FROM export_files" in self.sql:
            return self.rows.get("export_files", [])
        raise AssertionError(f"unexpected fetchall query: {self.sql}")


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def _install_fake_pg(monkeypatch, *, existing_tables, rows=None, fail_on=None):
    cursor = _FakeCursor(
        existing_tables=existing_tables,
        rows=rows,
        fail_on=fail_on,
    )
    monkeypatch.setattr(
        s3_gc.psycopg,
        "connect",
        lambda _dsn: _FakeConnection(cursor),
    )
    return cursor


class _FakeObjectsPaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, *, Bucket, Prefix):
        assert Bucket == self.client.bucket
        self.client.listed_prefixes.append(Prefix)
        yield {
            "Contents": [
                {
                    "Key": key,
                    "LastModified": datetime(2000, 1, 1, tzinfo=timezone.utc),
                }
                for key in self.client.keys
                if key.startswith(Prefix)
            ]
        }


class _FakeS3:
    def __init__(self, keys, bucket="robotics-data"):
        self.bucket = bucket
        self.keys = list(keys)
        self.listed_prefixes = []

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return _FakeObjectsPaginator(self)


OPTIONAL_REGISTRIES = {"parquet_files", "media_files", "qa_reports", "export_files"}
KNOWN_PREFIXES = {
    "raw/",
    "manifests/",
    "processed/parquet/",
    "processed/media/",
    "qa/reports/",
    "exports/",
}


def test_known_empty_registries_sweep_aged_orphans_but_cas_stays_unknown(monkeypatch):
    # A CAS-backed upload demonstrates why key membership and coverage are
    # different: the key is live, but this incomplete CAS lifecycle model is
    # still not permission to sweep anything in that namespace.
    _install_fake_pg(
        monkeypatch,
        existing_tables=OPTIONAL_REGISTRIES,
        rows={
            "uploads": [("s3://robotics-data/cas/files/live-sha",)],
            "manifests": [("manifests/robot-a/live.json",)],
        },
    )
    reachable = s3_gc.reachable_keys_from_pg("fake-dsn", "robotics-data")

    assert reachable == {"cas/files/live-sha", "manifests/robot-a/live.json"}
    assert reachable.covered_prefixes == KNOWN_PREFIXES
    assert s3_gc.reachability_known_for_prefix(reachable, "raw/robot-a/")
    assert not s3_gc.reachability_known_for_prefix(reachable, "cas/files/")

    orphan_keys = [
        "raw/mcap/orphan.mcap",
        "manifests/orphan.json",
        "processed/parquet/orphan.parquet",
        "processed/media/orphan.mp4",
        "qa/reports/orphan.json",
        "exports/orphan.parquet",
        "cas/files/orphan-sha",
    ]
    s3 = _FakeS3(orphan_keys)
    cfg = s3_gc.GCConfig(
        bucket="robotics-data",
        prefixes=[*sorted(KNOWN_PREFIXES), "cas/files/"],
        min_age_seconds=1,
        delete_limit=100,
        dry_run=True,
    )

    scanned, candidates, skipped = s3_gc.compute_delete_set(s3, cfg, reachable)

    assert scanned == 6
    assert {item["Key"] for item in candidates} == set(orphan_keys) - {"cas/files/orphan-sha"}
    assert skipped == ["cas/files/"]
    assert set(s3.listed_prefixes) == KNOWN_PREFIXES


def test_missing_optional_registry_is_unknown_not_known_empty(monkeypatch):
    _install_fake_pg(monkeypatch, existing_tables={"parquet_files"})
    reachable = s3_gc.reachable_keys_from_pg("fake-dsn", "robotics-data")

    assert reachable == set()
    assert reachable.covered_prefixes == {"raw/", "manifests/", "processed/parquet/"}

    s3 = _FakeS3(["processed/media/unregistered.mp4"])
    cfg = s3_gc.GCConfig(
        bucket="robotics-data",
        prefixes=["processed/media/"],
        min_age_seconds=1,
        delete_limit=100,
        dry_run=False,
    )
    scanned, candidates, skipped = s3_gc.compute_delete_set(s3, cfg, reachable)

    assert (scanned, candidates, skipped) == (0, [], ["processed/media/"])
    assert s3.listed_prefixes == []


def test_known_empty_sweep_preserves_global_delete_limit(monkeypatch):
    _install_fake_pg(monkeypatch, existing_tables=OPTIONAL_REGISTRIES)
    reachable = s3_gc.reachable_keys_from_pg("fake-dsn", "robotics-data")
    s3 = _FakeS3(
        [
            "raw/mcap/a.mcap",
            "raw/mcap/b.mcap",
            "processed/parquet/c.parquet",
        ]
    )
    cfg = s3_gc.GCConfig(
        bucket="robotics-data",
        prefixes=["raw/", "processed/parquet/"],
        min_age_seconds=1,
        delete_limit=2,
        dry_run=True,
    )

    scanned, candidates, skipped = s3_gc.compute_delete_set(s3, cfg, reachable)

    assert scanned == 2
    assert [item["Key"] for item in candidates] == ["raw/mcap/a.mcap", "raw/mcap/b.mcap"]
    assert skipped == []
    assert s3.listed_prefixes == ["raw/"]


def test_database_error_propagates_without_partial_empty_snapshot(monkeypatch):
    _install_fake_pg(
        monkeypatch,
        existing_tables=OPTIONAL_REGISTRIES,
        fail_on="SELECT parquet_path FROM processed_topics",
    )

    with pytest.raises(RuntimeError, match="fake database read failure"):
        s3_gc.reachable_keys_from_pg("fake-dsn", "robotics-data")


def test_durable_sessions_protect_parts_and_unpublished_assembled_object(monkeypatch):
    _install_fake_pg(
        monkeypatch,
        existing_tables={"upload_multipart_sessions"},
        rows={
            "uploads": [("s3://robotics-data/raw/pending.mcap",)],
            "upload_multipart_sessions": [
                ("s3://robotics-data/raw/pending.mcap", "saved-session"),
                ("s3://another-bucket/raw/pending.mcap", "foreign-session"),
            ],
        },
    )
    snapshot = s3_gc.reachable_keys_from_pg("fake-dsn", "robotics-data")
    assert "raw/pending.mcap" in snapshot
    assert snapshot.protected_mpus == {("raw/pending.mcap", "saved-session")}

    class MultipartStore:
        def __init__(self):
            self.aborted = []

        def get_paginator(self, operation):
            assert operation == "list_multipart_uploads"
            return self

        def paginate(self, **kwargs):
            yield {"Uploads": [
                {"Key": "raw/pending.mcap", "UploadId": session,
                 "Initiated": datetime(2000, 1, 1, tzinfo=timezone.utc)}
                for session in ("saved-session", "orphan-session")
            ]}

        def abort_multipart_upload(self, **kwargs):
            self.aborted.append(kwargs["UploadId"])

    storage = MultipartStore()
    count, candidates = s3_gc.reclaim_incomplete_mpus(
        storage, "robotics-data", 1, False, prefix="raw/",
        protected_mpus=snapshot.protected_mpus,
    )
    assert count == 1
    assert storage.aborted == ["orphan-session"]
    assert candidates == [{"Key": "raw/pending.mcap", "UploadId": "orphan-session"}]


def test_unavailable_session_registry_cannot_authorize_orphan_cleanup(monkeypatch):
    _install_fake_pg(
        monkeypatch,
        existing_tables={"upload_multipart_sessions"},
        fail_on="FROM upload_multipart_sessions",
    )
    with pytest.raises(RuntimeError, match="fake database read failure"):
        s3_gc.reachable_keys_from_pg("fake-dsn", "robotics-data")


def test_manifest_namespace_is_wired_into_every_default_gc_surface():
    config_text = (REPO_ROOT / "configs" / "gc.yaml").read_text(encoding="utf-8")
    dev_compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    release_compose = (
        REPO_ROOT / "deploy" / "docker-compose.release.yml"
    ).read_text(encoding="utf-8")

    assert '- "manifests/"' in config_text
    assert "GC_PREFIXES=${GC_PREFIXES:-raw/,manifests/," in dev_compose
    assert "GC_PREFIXES: ${GC_PREFIXES:-raw/,manifests/," in release_compose


@pytest.mark.parametrize(
    ("name", "bad_value"),
    [
        ("GC_DRY_RUN", "tru"),
        ("GC_DRY_RUN", "1"),
        ("GC_DRY_RUN", " true"),
        ("GC_MIN_AGE_HOURS", "0"),
        ("GC_MIN_AGE_HOURS", "-1"),
        ("GC_MIN_AGE_HOURS", "nan"),
        ("GC_MPU_MAX_AGE_HOURS", "-1"),
        ("GC_DELETE_LIMIT", "0"),
        ("GC_DELETE_LIMIT", "100001"),
        ("GC_INTERVAL_SECONDS", "0"),
        ("METRICS_PORT", "65536"),
        ("GC_PREFIXES", "raw/,"),
        ("GC_PREFIXES", "../"),
    ],
)
def test_invalid_runtime_gc_settings_fail_before_metrics_or_storage(
    monkeypatch, tmp_path, name, bad_value
):
    relevant = {
        "GC_DRY_RUN",
        "GC_MIN_AGE_HOURS",
        "GC_MPU_MAX_AGE_HOURS",
        "GC_DELETE_LIMIT",
        "GC_INTERVAL_SECONDS",
        "GC_PREFIXES",
        "METRICS_PORT",
    }
    for variable in relevant:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("S3_BUCKET", "robotics-data")
    monkeypatch.setenv("GC_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv(name, bad_value)
    monkeypatch.setattr(
        s3_gc,
        "start_http_server",
        lambda *_: pytest.fail("invalid config started the metrics server"),
    )

    assert s3_gc.main() == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("true", True), ("TRUE", True), ("false", False)],
)
def test_gc_dry_run_strict_parser_accepts_only_explicit_booleans(raw, expected):
    assert s3_gc.parse_strict_bool("GC_DRY_RUN", raw) is expected
