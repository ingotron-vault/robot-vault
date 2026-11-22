"""Static wiring guard for the `media_files` GC integration.

`tests/integration/test_gc_reachability.py` proves the *behavior* (a
media_files-registered object survives a real GC sweep, an orphan under the
same prefix is deleted) against a live stack, but that suite gates nothing
in CI -- it is skipped wholesale whenever psycopg/boto3/Postgres/object store are
unavailable (see its own module docstring and
`phases/05-query-service/tests/unit/test_single_gc_invariant.py`, which
states the same rule for the single-GC invariant). This file is the CI-gated
half: pure source/AST/YAML checks, no Postgres, no S3, no network, so a
future refactor that silently drops the `media_files` reachability source
(or the `processed/media/` prefix from one of the three places it has to be
listed) fails a test that actually runs in `make test-unit` /
`.github/workflows/core-ci.yml`, instead of only rotting the integration test.

Mirrors `phases/common-config/tests/unit/test_episode_status.py`'s
migration-parsing style for the `pii_status` CHECK-literal check.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[4]
S3_GC = REPO_ROOT / "tools" / "s3_gc.py"
BASELINE = REPO_ROOT / "db" / "migrations" / "001_baseline.sql"
GC_YAML = REPO_ROOT / "configs" / "gc.yaml"
COMPOSE_YML = REPO_ROOT / "docker-compose.yml"


def _s3_gc_source() -> str:
    assert S3_GC.is_file(), f"missing {S3_GC}"
    return S3_GC.read_text()


def _table_section(table: str) -> str:
    """The baseline's CREATE TABLE for `table`, plus its indexes and triggers.

    The section ends where the next table or schema area starts.
    """
    sql = BASELINE.read_text()
    start = sql.index(f"CREATE TABLE {table} (")
    ends = [
        position
        for position in (
            sql.find("\nCREATE TABLE ", start + 1),
            sql.find("\n-- ====", start + 1),
            sql.find("\nCREATE FUNCTION ", start + 1),
        )
        if position != -1
    ]
    return sql[start:min(ends)] if ends else sql[start:]


def _media_files_sql() -> str:
    return _table_section("media_files")


def _top_level_function_body(source: str, def_needle: str) -> str:
    """Slice out one top-level function's source, from `def_needle` up to
    (but not including) the next top-level `def `/`class `/`@dataclass`.

    Only matches at column 0 (`\\n(?=def |class |@dataclass)`, not
    `re.MULTILINE`), which is exactly what distinguishes a top-level
    definition from an indented, nested one -- so this can't accidentally
    stop at a nested `def` inside the target function itself.
    """
    start = source.index(def_needle)
    rest = source[start:]
    m = re.search(r"\n(?=def |class |@dataclass)", rest[1:])
    end = (m.start() + 1) if m else len(rest)
    return rest[:end]


# --- (a) media_files reachability source, guarded ---------------------------


def test_media_files_select_exists_with_to_regclass_guard():
    body = _top_level_function_body(_s3_gc_source(), "def reachable_keys_from_pg(")

    assert "to_regclass('public.media_files')" in body, (
        "reachable_keys_from_pg must probe for media_files' existence with "
        "the same to_regclass guard used for parquet_files, so "
        "this script keeps working against a database that has no "
        "media_files table yet -- the registry table must exist before any "
        "service writes a byte to the new prefix"
    )
    assert re.search(
        r"SELECT\s+key\s+FROM\s+media_files\s+AS\s+output\b.*?"
        r"WHERE\s+key\s+IS\s+NOT\s+NULL\s+AND\s+key\s*<>\s*''",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ), "reachable_keys_from_pg must select `key` from media_files as a reachability source"
    assert "output.attempt_id = published.attempt_id" in body
    assert "output.attempt_id = live.attempt_id" in body
    assert "episode.attempt_id = published.attempt_id" in body
    assert "episode.attempt_id = live.attempt_id" in body
    assert "live.lease_expires_at > NOW()" in body

    # The guard must actually gate the SELECT (appear textually before it),
    # not merely exist somewhere else in the function.
    assert body.index("to_regclass('public.media_files')") < body.index("FROM media_files"), (
        "the to_regclass('public.media_files') probe must precede the "
        "`SELECT key FROM media_files` it guards"
    )


# --- (b) processed/media/ in main()'s hardcoded fallback --------------------


def test_processed_media_prefix_in_main_fallback_default():
    main_body = _top_level_function_body(_s3_gc_source(), "def main(")
    assert "'processed/media/'" in main_body or '"processed/media/"' in main_body, (
        "main()'s hardcoded prefix fallback must include 'processed/media/' "
        "so a deployment with no GC_PREFIXES env var and no configs/gc.yaml "
        "still sweeps the new prefix once media_files is populated"
    )


# --- configs/gc.yaml and docker-compose.yml also list the prefix -----------


def test_gc_yaml_lists_processed_media_prefix():
    assert GC_YAML.is_file(), f"missing {GC_YAML}"
    y = yaml.safe_load(GC_YAML.read_text()) or {}
    prefixes = ((y.get("gc") or {}).get("prefixes")) or []
    assert "processed/media/" in prefixes, (
        f"configs/gc.yaml gc.prefixes is missing 'processed/media/': {prefixes}"
    )


def test_docker_compose_gc_prefixes_default_includes_processed_media():
    assert COMPOSE_YML.is_file(), f"missing {COMPOSE_YML}"
    compose = yaml.safe_load(COMPOSE_YML.read_text()) or {}
    gc_service = (compose.get("services") or {}).get("gc") or {}
    env = gc_service.get("environment") or []
    gc_prefix_entries = [e for e in env if isinstance(e, str) and e.startswith("GC_PREFIXES=")]
    assert gc_prefix_entries, "docker-compose.yml gc service has no GC_PREFIXES environment entry"
    assert "processed/media/" in gc_prefix_entries[0], (
        f"docker-compose.yml GC_PREFIXES default is missing 'processed/media/': {gc_prefix_entries[0]}"
    )


# --- media_files pii_status CHECK literals ----------------------------------


def test_media_files_pii_status_check_matches_canonical_vocabulary():
    """The database's copy of the pii_status vocabulary must equal the set
    the baseline schema documents: ('raw', 'scrubbed', 'not_applicable')."""
    matches = re.findall(
        r"CHECK\s*\(pii_status\s+IN\s+\(([^)]*)\)",
        _media_files_sql(),
        flags=re.IGNORECASE,
    )
    assert len(matches) == 1, "media_files must have one pii_status value list"
    listed = re.findall(r"'([^']*)'", matches[0])
    expected = ["raw", "scrubbed", "not_applicable"]
    assert listed == expected, f"the baseline lists {listed}, expected {expected}"


def test_media_files_key_is_unique_and_episode_fk_cascades():
    sql = _media_files_sql()
    assert re.search(r"key\s+TEXT\s+UNIQUE\s+NOT\s+NULL", sql, flags=re.IGNORECASE), (
        "media_files.key must be UNIQUE NOT NULL -- it is the join key the "
        "GC reachability source reads"
    )
    assert re.search(
        r"episode_id\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+episodes\s*\(\s*episode_id\s*\)\s+ON\s+DELETE\s+CASCADE",
        sql,
        flags=re.IGNORECASE,
    ), "media_files.episode_id must FK to episodes(episode_id) ON DELETE CASCADE"
