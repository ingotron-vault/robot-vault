"""Release-only, read-only operational metrics from bounded local sources.

The service has no Docker socket, S3 credential, private key, or write mount.
Every label comes from this module's fixed inventory rather than database rows.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Mapping

import psycopg
from cryptography import x509


LOGGER = logging.getLogger(__name__)
VOLUME_NAMES = ("database", "object_store", "uploads", "processor_spool", "exporter_spool")
CERTIFICATE_NAMES = ("ingest", "query")
SOURCES = ("volumes", "database", "backup", "certificates")
BACKUP_NAMES = (
    "robot_vault_backup_last_success_timestamp_seconds",
    "robot_vault_backup_last_failure_timestamp_seconds",
    "robot_vault_backup_last_run_success",
)
BACKUP_LINE = re.compile(r"^([a-z_]+) ([0-9]+)$")
CERTIFICATE_PEM = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


PROCESSING_BACKLOG_SQL = """
SELECT COUNT(*),
       COALESCE(EXTRACT(EPOCH FROM NOW() - MIN(COALESCE(u.upload_completed_at, u.created_at))), 0)
FROM uploads u
LEFT JOIN processed p ON p.upload_id = u.id
LEFT JOIN processing_status ps ON ps.upload_id = u.id
WHERE u.status = 'completed' AND u.integrity_verified IS TRUE
  AND p.upload_id IS NULL
  AND COALESCE(ps.attempts, 0) < %s
  AND (ps.upload_id IS NULL OR ps.status IN ('pending', 'failed')
       OR (ps.status = 'processing' AND
           COALESCE(ps.lease_expires_at, '-infinity'::timestamptz) <= NOW()))
"""
EXPORT_BACKLOG_SQL = """
SELECT COUNT(*), COALESCE(EXTRACT(EPOCH FROM NOW() - MIN(created_at)), 0)
FROM export_status
WHERE attempts < %s
  AND (status IN ('pending', 'failed')
       OR (status = 'exporting' AND
           COALESCE(lease_expires_at, '-infinity'::timestamptz) <= NOW()))
"""


def parse_backup_text(path: Path, now: float) -> dict[str, int]:
    """Read only the three numeric backup facts; refuse stale/malformed input."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("backup metrics file is absent or invalid")
    values: dict[str, int] = {}
    for raw_line in path.read_text(encoding="ascii").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        match = BACKUP_LINE.fullmatch(raw_line)
        if match is None or match.group(1) not in BACKUP_NAMES or match.group(1) in values:
            raise ValueError("backup metrics file contains an unexpected sample")
        values[match.group(1)] = int(match.group(2))
    if set(values) != set(BACKUP_NAMES) or values["robot_vault_backup_last_run_success"] not in (0, 1):
        raise ValueError("backup metrics file is incomplete")
    for name in BACKUP_NAMES[:2]:
        if values[name] > now + 300:
            raise ValueError("backup timestamp is in the future")
    if path.stat().st_mtime > now + 300:
        raise ValueError("backup metrics file mtime is in the future")
    return values


def certificate_expiry(path: Path) -> float:
    """Earliest expiry in the deployed public chain, including intermediates."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError("public certificate chain is absent or invalid")
    raw = path.read_bytes()
    matches = CERTIFICATE_PEM.findall(raw)
    if not matches or CERTIFICATE_PEM.sub(b"", raw).strip():
        raise ValueError("public certificate chain contains non-certificate material")
    return min(cert.not_valid_after_utc.timestamp() for cert in map(x509.load_pem_x509_certificate, matches))


def _sample(name: str, value: int | float, labels: Mapping[str, str] | None = None) -> str:
    if labels:
        fixed = ",".join(f'{key}="{value}"' for key, value in labels.items())
        return f"{name}{{{fixed}}} {value}\n"
    return f"{name} {value}\n"


class OperationalCollector:
    """A scrape snapshots each source independently and exposes failed reads."""

    def __init__(
        self,
        *,
        database_url: str,
        volume_paths: Mapping[str, Path],
        certificate_paths: Mapping[str, Path],
        backup_path: Path,
        edition: str,
        processor_max_attempts: int = 3,
        exporter_max_attempts: int = 3,
        clock: Callable[[], float] = time.time,
        connect: Callable = psycopg.connect,
        statvfs: Callable = os.statvfs,
    ) -> None:
        if edition not in ("core", "pro"):
            raise ValueError("edition must be core or pro")
        if set(volume_paths) != set(VOLUME_NAMES):
            raise ValueError("volume inventory must be fixed")
        if set(certificate_paths) != set(CERTIFICATE_NAMES):
            raise ValueError("certificate inventory must be fixed")
        self.database_url = database_url
        self.volume_paths = volume_paths
        self.certificate_paths = certificate_paths
        self.backup_path = backup_path
        self.edition = edition
        self.processor_max_attempts = processor_max_attempts
        self.exporter_max_attempts = exporter_max_attempts
        self.clock = clock
        self.connect = connect
        self.statvfs = statvfs
        self.last_success = {source: 0.0 for source in SOURCES}
        self._lock = threading.Lock()

    def _volumes(self) -> str:
        lines = []
        for name in VOLUME_NAMES:
            stats = self.statvfs(self.volume_paths[name])
            if stats.f_blocks <= 0 or stats.f_frsize <= 0:
                raise ValueError("volume has no measurable capacity")
            labels = {"volume": name}
            lines.append(_sample("robot_vault_volume_available_bytes", stats.f_bavail * stats.f_frsize, labels))
            lines.append(_sample("robot_vault_volume_total_bytes", stats.f_blocks * stats.f_frsize, labels))
        return "".join(lines)

    def _database(self) -> str:
        lines = []
        with self.connect(
            self.database_url,
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=3000",
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(PROCESSING_BACKLOG_SQL, (self.processor_max_attempts,))
                processing_count, processing_age = cursor.fetchone()
                backlog = [("processing", processing_count, processing_age)]
                if self.edition == "pro":
                    cursor.execute(EXPORT_BACKLOG_SQL, (self.exporter_max_attempts,))
                    export_count, export_age = cursor.fetchone()
                    backlog.append(("export", export_count, export_age))
        for queue, count, age in backlog:
            labels = {"queue": queue}
            lines.append(_sample("robot_vault_backlog_items", int(count), labels))
            lines.append(_sample("robot_vault_backlog_age_seconds", max(0, float(age)), labels))
        return "".join(lines)

    def _backup(self, now: float) -> str:
        values = parse_backup_text(self.backup_path, now)
        return (
            _sample("robot_vault_backup_last_success_timestamp_seconds", values[BACKUP_NAMES[0]])
            + _sample("robot_vault_backup_last_failure_timestamp_seconds", values[BACKUP_NAMES[1]])
            + _sample("robot_vault_backup_last_run_success", values[BACKUP_NAMES[2]])
        )

    def _certificates(self) -> str:
        return "".join(
            _sample("robot_vault_certificate_expiry_timestamp_seconds", certificate_expiry(self.certificate_paths[name]), {"certificate": name})
            for name in CERTIFICATE_NAMES
        )

    def collect(self) -> str:
        with self._lock:
            now = self.clock()
            lines = ["# Robot Vault operational metrics; fixed labels and payload only.\n"]
            for source, collector in (
                ("volumes", self._volumes),
                ("database", self._database),
                ("backup", lambda: self._backup(now)),
                ("certificates", self._certificates),
            ):
                success = 0
                try:
                    payload = collector()
                except Exception as exc:
                    LOGGER.warning("operational metrics %s collection failed: %s", source, type(exc).__name__)
                else:
                    success = 1
                    self.last_success[source] = now
                    lines.append(payload)
                labels = {"source": source}
                lines.append(_sample("robot_vault_operational_collection_success", success, labels))
                lines.append(_sample("robot_vault_operational_last_success_timestamp_seconds", self.last_success[source], labels))
            return "".join(lines)


def serve(collector: OperationalCollector, port: int = 9110) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/metrics":
                self.send_error(404)
                return
            payload = collector.collect().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    # One internal Prometheus scraper. A serial server bounds concurrent SQL
    # work and threads even when a client retries a slow request.
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def worker_attempt_bounds(environ: Mapping[str, str]) -> tuple[int, int]:
    """Use the processor's clamp and exporter's invalid-value fallback."""
    processor = max(1, int(environ.get("PROCESSOR_MAX_ATTEMPTS", "3")))
    try:
        exporter = int(environ.get("EXPORTER_MAX_ATTEMPTS", "3"))
    except ValueError:
        exporter = 3
    return processor, exporter if exporter >= 1 else 3


def main() -> None:
    processor_attempts, exporter_attempts = worker_attempt_bounds(os.environ)
    collector = OperationalCollector(
        database_url=os.environ["OPS_DATABASE_URL"],
        volume_paths={name: Path(f"/run/robot-vault/volumes/{name}") for name in VOLUME_NAMES},
        certificate_paths={name: Path(f"/run/robot-vault/tls/{name}.crt") for name in CERTIFICATE_NAMES},
        backup_path=Path("/run/robot-vault/backup-metrics/robot-vault-backup.prom"),
        edition=os.environ["ROBOT_VAULT_EDITION"],
        processor_max_attempts=processor_attempts,
        exporter_max_attempts=exporter_attempts,
    )
    serve(collector)


if __name__ == "__main__":
    main()
