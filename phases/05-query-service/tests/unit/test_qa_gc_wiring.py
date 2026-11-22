"""Static wiring guard for the `qa_reports` GC integration.

`tests/integration/test_gc_reachability.py` proves the *behavior* (a
qa_reports-registered object survives a real GC sweep, an orphan under the
same prefix is deleted) against a live stack, but that suite gates nothing
in CI -- it is skipped wholesale whenever psycopg/boto3/Postgres/object store are
unavailable (see its own module docstring). This file is the CI-gated half:
pure source/AST/YAML checks, no Postgres, no S3, no network, so a future
refactor that silently drops the `qa_reports` reachability source (or the
`qa/reports/` prefix from one of the three places it has to be listed)
fails a test that actually runs in `make test-unit` /
`.github/workflows/core-ci.yml`, instead of only rotting the integration test.

Mirrors `phases/05-query-service/tests/unit/test_media_gc_wiring.py`'s
approach for the `media_files` registry, and
`phases/common-config/tests/unit/test_episode_status.py`'s migration-parsing
style for CHECK-literal checks.
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


def _qa_reports_sql() -> str:
    return _table_section("qa_reports")


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


# --- (a) qa_reports reachability source, guarded ----------------------------


def test_qa_reports_select_exists_with_to_regclass_guard():
    body = _top_level_function_body(_s3_gc_source(), "def reachable_keys_from_pg(")

    assert "to_regclass('public.qa_reports')" in body, (
        "reachable_keys_from_pg must probe for qa_reports' existence with "
        "the same to_regclass guard used for parquet_files/media_files, so "
        "this script keeps working against a database that has no "
        "qa_reports table yet -- the registry table must exist before any "
        "service writes a byte to the new prefix"
    )
    assert re.search(
        r"SELECT\s+detail_key\s+FROM\s+qa_reports\s+AS\s+output\b.*?"
        r"WHERE\s+detail_key\s+IS\s+NOT\s+NULL\s+AND\s+detail_key\s*<>\s*''",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ), "reachable_keys_from_pg must select `detail_key` from qa_reports as a reachability source"
    assert "output.attempt_id = published.attempt_id" in body
    assert "output.attempt_id = live.attempt_id" in body
    assert "episode.attempt_id = published.attempt_id" in body
    assert "episode.attempt_id = live.attempt_id" in body
    assert "live.lease_expires_at > NOW()" in body

    # The guard must actually gate the SELECT (appear textually before it),
    # not merely exist somewhere else in the function.
    assert body.index("to_regclass('public.qa_reports')") < body.index("FROM qa_reports"), (
        "the to_regclass('public.qa_reports') probe must precede the "
        "`SELECT detail_key FROM qa_reports` it guards"
    )


# --- (b) qa/reports/ in main()'s hardcoded fallback -------------------------


def test_qa_reports_prefix_in_main_fallback_default():
    main_body = _top_level_function_body(_s3_gc_source(), "def main(")
    assert "'qa/reports/'" in main_body or '"qa/reports/"' in main_body, (
        "main()'s hardcoded prefix fallback must include 'qa/reports/' so a "
        "deployment with no GC_PREFIXES env var and no configs/gc.yaml still "
        "sweeps the new prefix once qa_reports is populated"
    )


# --- configs/gc.yaml and docker-compose.yml also list the prefix -----------


def test_gc_yaml_lists_qa_reports_prefix():
    assert GC_YAML.is_file(), f"missing {GC_YAML}"
    y = yaml.safe_load(GC_YAML.read_text()) or {}
    prefixes = ((y.get("gc") or {}).get("prefixes")) or []
    assert "qa/reports/" in prefixes, (
        f"configs/gc.yaml gc.prefixes is missing 'qa/reports/': {prefixes}"
    )


def test_docker_compose_gc_prefixes_default_includes_qa_reports():
    assert COMPOSE_YML.is_file(), f"missing {COMPOSE_YML}"
    compose = yaml.safe_load(COMPOSE_YML.read_text()) or {}
    gc_service = (compose.get("services") or {}).get("gc") or {}
    env = gc_service.get("environment") or []
    gc_prefix_entries = [e for e in env if isinstance(e, str) and e.startswith("GC_PREFIXES=")]
    assert gc_prefix_entries, "docker-compose.yml gc service has no GC_PREFIXES environment entry"
    assert "qa/reports/" in gc_prefix_entries[0], (
        f"docker-compose.yml GC_PREFIXES default is missing 'qa/reports/': {gc_prefix_entries[0]}"
    )


# --- qa_reports scope CHECK literals ----------------------------------------


def test_qa_reports_scope_check_matches_canonical_vocabulary():
    """The database's copy of the qa_reports.scope vocabulary must equal the
    set the baseline schema documents: ('upload', 'episode')."""
    matches = re.findall(
        r"CHECK\s*\(scope\s+IN\s+\(([^)]*)\)",
        _qa_reports_sql(),
        flags=re.IGNORECASE,
    )
    assert len(matches) == 1, "qa_reports must have one scope value list"
    listed = re.findall(r"'([^']*)'", matches[0])
    expected = ["upload", "episode"]
    assert listed == expected, f"the baseline lists {listed}, expected {expected}"


def test_qa_reports_scope_check_is_an_xor():
    """A report has exactly one owner: the scope-matching FK, never both."""
    assert re.search(
        r"CHECK\s*\(\s*\(\s*scope\s*=\s*'upload'\s+AND\s+upload_id\s+IS\s+NOT\s+NULL\s+"
        r"AND\s+episode_id\s+IS\s+NULL\s*\)\s*OR\s*"
        r"\(\s*scope\s*=\s*'episode'\s+AND\s+episode_id\s+IS\s+NOT\s+NULL\s+"
        r"AND\s+upload_id\s+IS\s+NULL\s*\)\s*\)",
        _qa_reports_sql(),
        flags=re.IGNORECASE,
    ), (
        "qa_reports must require the scope-matching FK and explicitly "
        "require the other FK to be NULL"
    )


def test_qa_reports_has_no_updated_at_trigger():
    """qa_reports is append-only (re-scores add rows; latest-by-created_at
    wins) -- unlike episodes/media_files, it must NOT get an
    update_updated_at_column() trigger."""
    sql = _qa_reports_sql()
    assert "update_qa_reports_updated_at" not in sql, (
        "qa_reports is append-only and must not have an updated_at trigger"
    )
    assert "updated_at" not in re.sub(r"--[^\n]*", "", sql), (
        "qa_reports must not have an updated_at column -- it is append-only"
    )


def test_qa_reports_fks_cascade_and_indexes_present():
    sql = _qa_reports_sql()
    assert re.search(
        r"upload_id\s+UUID\s+REFERENCES\s+uploads\s*\(\s*id\s*\)\s+ON\s+DELETE\s+CASCADE",
        sql,
        flags=re.IGNORECASE,
    ), "qa_reports.upload_id must FK to uploads(id) ON DELETE CASCADE"
    assert re.search(
        r"episode_id\s+UUID\s+REFERENCES\s+episodes\s*\(\s*episode_id\s*\)\s+ON\s+DELETE\s+CASCADE",
        sql,
        flags=re.IGNORECASE,
    ), "qa_reports.episode_id must FK to episodes(episode_id) ON DELETE CASCADE"
    assert re.search(
        r"CREATE INDEX \w+ ON qa_reports\(episode_id,\s*created_at DESC\)",
        sql,
        flags=re.IGNORECASE,
    ), "qa_reports needs an (episode_id, created_at DESC) index"
    assert re.search(
        r"CREATE INDEX \w+ ON qa_reports\(upload_id,\s*created_at DESC\)",
        sql,
        flags=re.IGNORECASE,
    ), "qa_reports needs an (upload_id, created_at DESC) index"
