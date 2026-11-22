"""Fast, database-free guards for migration discovery and identity."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migrate  # noqa: E402


def _write(path: Path, content: str = "SELECT 1;\n") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_discovery_sorts_by_filename_and_returns_exact_versions(tmp_path: Path):
    _write(tmp_path / "010_tenth.sql")
    _write(tmp_path / "002_second.sql")
    _write(tmp_path / "README.txt")

    discovered = migrate.discover_migrations(tmp_path)

    assert [(version, path.name) for version, path in discovered] == [
        ("002", "002_second.sql"),
        ("010", "010_tenth.sql"),
    ]


def test_discovery_rejects_duplicate_numeric_versions(tmp_path: Path):
    _write(tmp_path / "001_first.sql")
    _write(tmp_path / "1_same_version.sql")

    with pytest.raises(migrate.DuplicateMigrationError, match="duplicate migration version"):
        migrate.discover_migrations(tmp_path)


def test_discovery_rejects_duplicate_names_case_insensitively(tmp_path: Path):
    _write(tmp_path / "001_create_widgets.sql")
    _write(tmp_path / "002_CREATE_WIDGETS.sql")

    with pytest.raises(migrate.DuplicateMigrationError, match="duplicate migration name"):
        migrate.discover_migrations(tmp_path)


def test_discovery_rejects_malformed_sql_filenames(tmp_path: Path):
    _write(tmp_path / "migration_without_a_version.sql")

    with pytest.raises(migrate.InvalidMigrationNameError, match="NNN_name.sql"):
        migrate.discover_migrations(tmp_path)


def test_checksum_hashes_exact_bytes(tmp_path: Path):
    path = tmp_path / "001_bytes.sql"
    payload = b"SELECT '\r\n';\r\n"
    path.write_bytes(payload)

    assert migrate.migration_checksum(path) == hashlib.sha256(payload).hexdigest()


def test_direct_apply_rejects_a_version_filename_mismatch(tmp_path: Path):
    path = _write(tmp_path / "001_probe.sql")

    with pytest.raises(migrate.MigrationIntegrityError, match="does not match"):
        migrate.apply_migration(object(), "002", path)  # type: ignore[arg-type]
