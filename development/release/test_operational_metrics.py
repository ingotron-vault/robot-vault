"""Operational source collection, including failure and credential boundaries."""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tools.operational_metrics import (
    OperationalCollector,
    certificate_expiry,
    parse_backup_text,
    worker_attempt_bounds,
)


def _backup(path, *, success=1000, failure=0, last_run=1):
    path.write_text(
        "# HELP ignored explanatory text\n"
        f"robot_vault_backup_last_success_timestamp_seconds {success}\n"
        f"robot_vault_backup_last_failure_timestamp_seconds {failure}\n"
        f"robot_vault_backup_last_run_success {last_run}\n"
    )


def _cert(expiry):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "release.example")])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(expiry)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def test_backup_parser_distinguishes_no_success_from_unreadable(tmp_path):
    path = tmp_path / "backup.prom"
    now = time.time()
    _backup(path, success=0, last_run=0)
    assert parse_backup_text(path, now)["robot_vault_backup_last_success_timestamp_seconds"] == 0
    _backup(path, success=int(now) + 9999)
    with pytest.raises(ValueError, match="future"):
        parse_backup_text(path, now)
    path.write_text("robot_vault_backup_last_success_timestamp_seconds 1\n")
    with pytest.raises(ValueError, match="incomplete"):
        parse_backup_text(path, now)
    path.unlink()
    with pytest.raises(ValueError, match="absent"):
        parse_backup_text(path, now)


def test_backup_parser_rejects_old_names_extra_line_and_bad_flag(tmp_path):
    path = tmp_path / "robot-vault-backup.prom"
    now = time.time()
    good = (
        "robot_vault_backup_last_success_timestamp_seconds 1000\n"
        "robot_vault_backup_last_failure_timestamp_seconds 0\n"
        "robot_vault_backup_last_run_success 1\n"
    )
    path.write_text(good)
    assert parse_backup_text(path, now)["robot_vault_backup_last_run_success"] == 1
    path.write_text(good.replace("robot_vault_", "ingotron_"))
    with pytest.raises(ValueError, match="unexpected"):
        parse_backup_text(path, now)
    path.write_text(good + "robot_vault_backup_last_run_success 1\n")
    with pytest.raises(ValueError, match="unexpected"):
        parse_backup_text(path, now)
    path.write_text(good + "robot_vault_backup_extra 1\n")
    with pytest.raises(ValueError, match="unexpected"):
        parse_backup_text(path, now)
    path.write_text(good.replace("run_success 1", "run_success 2"))
    with pytest.raises(ValueError, match="incomplete"):
        parse_backup_text(path, now)


def test_certificate_chain_uses_earliest_member_and_rejects_key_material(tmp_path):
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=5)
    late = now + timedelta(days=40)
    path = tmp_path / "chain.pem"
    path.write_bytes(_cert(late) + _cert(soon))
    assert certificate_expiry(path) == pytest.approx(soon.timestamp(), abs=2)
    path.write_bytes(path.read_bytes() + b"-----BEGIN PRIVATE KEY-----\nsecret\n")
    with pytest.raises(ValueError, match="non-certificate"):
        certificate_expiry(path)


class Cursor:
    def __init__(self):
        self.sql = []
        self.rows = iter([(2, 3601.0), (1, 7201.0)])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.sql.append((query, params))

    def fetchone(self):
        return next(self.rows)


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self._cursor


def _collector(tmp_path, connect, *, edition="pro", now=None):
    now = now or time.time()
    backup = tmp_path / "backup.prom"
    _backup(backup, success=int(now) - 3600)
    cert = tmp_path / "cert.pem"
    cert.write_bytes(_cert(datetime.now(timezone.utc) + timedelta(days=30)))
    return OperationalCollector(
        database_url="postgresql://robot_vault_ops:placeholder@postgres:5432/vault",
        volume_paths={name: tmp_path for name in (
            "database", "object_store", "uploads", "processor_spool", "exporter_spool")},
        certificate_paths={"ingest": cert, "query": cert},
        backup_path=backup,
        edition=edition,
        clock=lambda: now,
        connect=connect,
        statvfs=lambda path: SimpleNamespace(f_bavail=10, f_frsize=4096, f_blocks=100),
    )


def test_database_backlog_collection_uses_read_only_bounded_sql(tmp_path):
    cursor = Cursor()
    calls = []

    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return Connection(cursor)

    output = _collector(tmp_path, connect).collect()
    assert 'robot_vault_backlog_items{queue="processing"} 2' in output
    assert 'robot_vault_backlog_age_seconds{queue="export"} 7201.0' in output
    assert 'robot_vault_operational_collection_success{source="database"} 1' in output
    assert calls[0][1]["connect_timeout"] == 3
    assert "default_transaction_read_only=on" in calls[0][1]["options"]
    assert cursor.sql[0][0] == "SET TRANSACTION READ ONLY"
    assert "integrity_verified IS TRUE" in cursor.sql[1][0]
    assert "lease_expires_at" in cursor.sql[2][0]


def test_failed_database_collection_has_no_false_zero_backlog(tmp_path):
    def broken(*args, **kwargs):
        raise OSError("credential is not logged")

    output = _collector(tmp_path, broken).collect()
    assert 'robot_vault_operational_collection_success{source="database"} 0' in output
    assert 'robot_vault_operational_last_success_timestamp_seconds{source="database"} 0.0' in output
    assert "robot_vault_backlog_age_seconds" not in output
    assert "credential is not logged" not in output


def test_core_edition_does_not_query_or_emit_export_backlog(tmp_path):
    cursor = Cursor()
    output = _collector(tmp_path, lambda *args, **kwargs: Connection(cursor), edition="core").collect()
    assert 'queue="export"' not in output
    assert len(cursor.sql) == 2


def test_attempt_bounds_match_worker_rules_and_compose_wiring():
    assert worker_attempt_bounds({}) == (3, 3)
    assert worker_attempt_bounds({"PROCESSOR_MAX_ATTEMPTS": "0", "EXPORTER_MAX_ATTEMPTS": "0"}) == (1, 3)
    assert worker_attempt_bounds({"PROCESSOR_MAX_ATTEMPTS": "6", "EXPORTER_MAX_ATTEMPTS": "invalid"}) == (6, 3)
    compose = (Path(__file__).resolve().parents[2] / "deploy/docker-compose.release.yml").read_text()
    assert compose.count("PROCESSOR_MAX_ATTEMPTS: ${PROCESSOR_MAX_ATTEMPTS:-3}") == 2
    # ops-metrics only: the exporter (and its own default) is in the Pro release overlay.
    assert compose.count("EXPORTER_MAX_ATTEMPTS: ${EXPORTER_MAX_ATTEMPTS:-3}") == 1
