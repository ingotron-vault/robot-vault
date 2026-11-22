#!/usr/bin/env python3
"""Robot Vault ordered, integrity-checked Postgres migration runner.

Applies ``db/migrations/NNN_name.sql`` files in filename order to the database
named by ``DATABASE_URL``.  Each migration is applied in its own transaction;
the version, filename, and SHA-256 of the exact SQL bytes are recorded in the
same transaction in ``schema_version``.

The runner holds one session-level Postgres advisory lock from schema-version
table setup through the final migration.  That makes the read/check/apply
sequence single-writer even when multiple service replicas start their
one-shot migrator concurrently.

A renamed, removed, or edited applied migration fails closed. Missing
integrity metadata also fails closed rather than being guessed at.

Usage::

    DATABASE_URL=postgres://user:pass@host:5432/dbname python db/migrate.py
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:
    import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
VERSION_RE = re.compile(r"^(\d+)_([^/]+)\.sql$")

# ASCII ``RV_MIGR8`` interpreted as a positive signed int64.  Advisory locks
# are scoped by Postgres to the current database, so independent Robot Vault
# databases on one cluster do not block each other.
MIGRATION_ADVISORY_LOCK_KEY = int.from_bytes(b"RV_MIGR8", "big", signed=True)


class MigrationError(RuntimeError):
    """Base class for migration discovery/integrity failures."""


class DuplicateMigrationError(MigrationError):
    """Two local migration files claim the same version or logical name."""


class InvalidMigrationNameError(MigrationError):
    """A SQL file in the migration directory doesn't follow NNN_name.sql."""


class MigrationIntegrityError(MigrationError):
    """Applied migration metadata no longer matches the local migration."""


def _load_psycopg():
    """Load the database driver only when a database operation is requested.

    Discovery and checksum validation are intentionally database-free, so they
    should remain runnable from a minimal checkout.  The supported Docker
    workflow installs the locked development requirements; this guard keeps a
    bare-host invocation actionable instead of failing while importing the
    offline migration tests.
    """
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "psycopg[binary] is required for database migrations; install "
            "requirements-dev.lock.txt or run `make test-unit`"
        ) from exc
    return psycopg


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    name: str | None
    checksum_sha256: str | None


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """Return unique ``(version, path)`` pairs sorted by filename.

    Versions are compared numerically, so ``001_alpha.sql`` and
    ``1_beta.sql`` are rejected as the same version.  Logical names are the
    filename portion after ``NNN_`` and are compared case-insensitively.  A
    repeated name under a new version is almost always a mistaken copy or
    renumber and is rejected before the database is touched.
    """
    if not migrations_dir.is_dir():
        return []

    migrations: list[tuple[str, Path]] = []
    versions: dict[int, Path] = {}
    names: dict[str, Path] = {}
    for path in migrations_dir.iterdir():
        if not path.is_file():
            continue
        match = VERSION_RE.fullmatch(path.name)
        if not match:
            if path.suffix.casefold() == ".sql":
                raise InvalidMigrationNameError(
                    f"invalid migration filename {path.name!r}; expected NNN_name.sql"
                )
            continue

        version = match.group(1)
        logical_name = match.group(2)
        numeric_version = int(version)
        normalized_name = logical_name.casefold()

        prior_version = versions.get(numeric_version)
        if prior_version is not None:
            raise DuplicateMigrationError(
                "duplicate migration version "
                f"{numeric_version}: {prior_version.name!r} and {path.name!r}"
            )
        prior_name = names.get(normalized_name)
        if prior_name is not None:
            raise DuplicateMigrationError(
                "duplicate migration name "
                f"{logical_name!r}: {prior_name.name!r} and {path.name!r}"
            )

        versions[numeric_version] = path
        names[normalized_name] = path
        migrations.append((version, path))

    migrations.sort(key=lambda version_path: version_path[1].name)
    return migrations


def migration_checksum(path: Path) -> str:
    """Return the SHA-256 hex digest of the migration's exact on-disk bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def acquire_migration_lock(conn: psycopg.Connection) -> None:
    """Wait for this database's one global Robot Vault migration lock."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))


def release_migration_lock(conn: psycopg.Connection) -> None:
    """Release the lock acquired by :func:`acquire_migration_lock`."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))


def ensure_schema_version_table(conn: psycopg.Connection) -> None:
    """Create the migration ledger with mandatory integrity metadata."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum_sha256 VARCHAR(64) NOT NULL
                        CONSTRAINT schema_version_checksum_sha256_check
                        CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_schema_version_name "
                "ON schema_version(name)"
            )


def get_applied_migrations(conn: psycopg.Connection) -> list[AppliedMigration]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, name, checksum_sha256 "
            "FROM schema_version ORDER BY version"
        )
        return [
            AppliedMigration(str(version), name, checksum)
            for version, name, checksum in cur.fetchall()
        ]


def verify_applied_migrations(
    conn: psycopg.Connection,
    migrations: Sequence[tuple[str, Path]],
) -> set[str]:
    """Verify the ledger against the local migration files.

    Returns the exact version strings already applied.  An applied row whose
    migration file is absent, renamed, or edited is an integrity error, and so
    is a row without its name or checksum.
    """
    local = {version: path for version, path in migrations}
    records = get_applied_migrations(conn)

    database_numeric_versions: dict[int, str] = {}
    for record in records:
        if not record.version.isdigit():
            raise MigrationIntegrityError(
                f"schema_version contains non-numeric version {record.version!r}"
            )
        numeric_version = int(record.version)
        prior = database_numeric_versions.get(numeric_version)
        if prior is not None:
            raise MigrationIntegrityError(
                "schema_version contains duplicate numeric version "
                f"{numeric_version}: {prior!r} and {record.version!r}"
            )
        database_numeric_versions[numeric_version] = record.version

        path = local.get(record.version)
        if path is None:
            raise MigrationIntegrityError(
                f"applied migration {record.version!r} has no matching local file"
            )

        expected_name = path.name
        expected_checksum = migration_checksum(path)
        if record.name is None or record.checksum_sha256 is None:
            raise MigrationIntegrityError(
                f"applied migration {record.version!r} has missing integrity metadata"
            )
        if record.name != expected_name:
            raise MigrationIntegrityError(
                f"applied migration {record.version!r} was recorded as "
                f"{record.name!r}, not local file {expected_name!r}"
            )
        if record.checksum_sha256 != expected_checksum:
            raise MigrationIntegrityError(
                f"checksum mismatch for applied migration {record.version!r} "
                f"({expected_name})"
            )

    applied_versions = {record.version for record in records}
    first_pending: str | None = None
    for version, _path in migrations:
        if version not in applied_versions:
            first_pending = first_pending or version
        elif first_pending is not None:
            raise MigrationIntegrityError(
                "schema_version has an out-of-order gap: migration "
                f"{first_pending!r} is pending but later migration "
                f"{version!r} is already applied"
            )

    return applied_versions


def apply_migration(conn: psycopg.Connection, version: str, path: Path) -> None:
    """Apply one migration and ledger its exact identity in one transaction."""
    match = VERSION_RE.fullmatch(path.name)
    if match is None:
        raise InvalidMigrationNameError(
            f"invalid migration filename {path.name!r}; expected NNN_name.sql"
        )
    if match.group(1) != version:
        raise MigrationIntegrityError(
            f"migration version {version!r} does not match filename {path.name!r}"
        )

    payload = path.read_bytes()
    try:
        sql = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationIntegrityError(
            f"migration {path.name!r} is not valid UTF-8"
        ) from exc
    checksum = hashlib.sha256(payload).hexdigest()

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_version (version, name, checksum_sha256) "
                "VALUES (%s, %s, %s)",
                (version, path.name, checksum),
            )


def run_migrations(
    database_url: str, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Verify history and apply all pending migrations under a global lock."""
    migrations = discover_migrations(migrations_dir)
    applied_now: list[str] = []
    psycopg = _load_psycopg()
    with psycopg.connect(database_url, autocommit=True) as conn:
        acquire_migration_lock(conn)
        try:
            ensure_schema_version_table(conn)
            already_applied = verify_applied_migrations(conn, migrations)
            for version, path in migrations:
                if version in already_applied:
                    continue
                apply_migration(conn, version, path)
                already_applied.add(version)
                applied_now.append(version)
        finally:
            # If the connection itself failed, Postgres releases session locks
            # when it closes.  Do not mask the causal migration/connection
            # exception with a best-effort explicit-unlock error.
            try:
                release_migration_lock(conn)
            except Exception:
                pass
    return applied_now


def main(argv: Iterable[str] | None = None) -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 1

    applied = run_migrations(database_url)
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("No pending migrations. Schema is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
