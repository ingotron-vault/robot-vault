"""Static wiring guard for the `datasets`/`export_status`/
`export_files` provenance-centerpiece registry and its `exports/` GC
integration.

`tests/integration/test_gc_reachability.py` proves the *behavior* (an
export_files-registered object survives a real GC sweep, an orphan under the
same prefix is deleted) against a live stack, but that suite gates nothing
in CI -- it is skipped wholesale whenever psycopg/boto3/Postgres/object store are
unavailable (see its own module docstring). This file is the CI-gated half:
pure source/AST/YAML checks, no Postgres, no S3, no network, so a future
refactor that silently drops the `export_files` reachability source (or the
`exports/` prefix from one of the three places it has to be listed, or the
immutability/lineage guarantees the baseline schema defines) fails a test that
actually runs in `make test-unit` / `.github/workflows/core-ci.yml`, instead of
only rotting the integration test.

Mirrors `phases/05-query-service/tests/unit/test_qa_gc_wiring.py`'s and
`test_media_gc_wiring.py`'s approach for the GC-wiring
half, and `phases/common-config/tests/unit/test_episode_status.py`'s
schema-parsing style for CHECK-literal checks.
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


def _datasets_area_sql() -> str:
    """The baseline's "Datasets (cuts) and exports" area: every table,
    function, trigger and index for datasets, dataset_episodes,
    dataset_evals, export_status and export_files."""
    sql = BASELINE.read_text()
    start = sql.index("-- Datasets (cuts) and exports")
    # The datasets area is the last section of the baseline.
    return sql[start:]


def _statement(sql: str, needle: str) -> str:
    """One CREATE TABLE statement, from `needle` up to its closing `);`."""
    start = sql.index(needle)
    return sql[start:sql.index("\n);", start) + 3]


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


# --- (a) export_files reachability source, guarded --------------------------


def test_export_files_select_exists_with_to_regclass_guard():
    body = _top_level_function_body(_s3_gc_source(), "def reachable_keys_from_pg(")

    assert "to_regclass('public.export_files')" in body, (
        "reachable_keys_from_pg must probe for export_files' existence with "
        "the same to_regclass guard used for parquet_files/media_files/"
        "qa_reports, so this script keeps working against a database that "
        "has no export_files table yet -- the registry table must exist "
        "before any service writes a byte to the new prefix"
    )
    assert re.search(
        r"SELECT\s+key\s+FROM\s+export_files\s+WHERE\s+key\s+IS\s+NOT\s+NULL\s+AND\s+key\s*<>\s*''",
        body,
        flags=re.IGNORECASE,
    ), "reachable_keys_from_pg must select `key` from export_files as a reachability source"

    # The guard must actually gate the SELECT (appear textually before it),
    # not merely exist somewhere else in the function.
    assert body.index("to_regclass('public.export_files')") < body.index("FROM export_files"), (
        "the to_regclass('public.export_files') probe must precede the "
        "`SELECT key FROM export_files` it guards"
    )


# --- (b) exports/ in main()'s hardcoded fallback -----------------------------


def test_exports_prefix_in_main_fallback_default():
    main_body = _top_level_function_body(_s3_gc_source(), "def main(")
    assert "'exports/'" in main_body or '"exports/"' in main_body, (
        "main()'s hardcoded prefix fallback must include 'exports/' so a "
        "deployment with no GC_PREFIXES env var and no configs/gc.yaml still "
        "sweeps the new prefix once export_files is populated"
    )


# --- configs/gc.yaml and docker-compose.yml also list the prefix -----------


def test_gc_yaml_lists_exports_prefix():
    assert GC_YAML.is_file(), f"missing {GC_YAML}"
    y = yaml.safe_load(GC_YAML.read_text()) or {}
    prefixes = ((y.get("gc") or {}).get("prefixes")) or []
    assert "exports/" in prefixes, (
        f"configs/gc.yaml gc.prefixes is missing 'exports/': {prefixes}"
    )


def test_docker_compose_gc_prefixes_default_includes_exports():
    assert COMPOSE_YML.is_file(), f"missing {COMPOSE_YML}"
    compose = yaml.safe_load(COMPOSE_YML.read_text()) or {}
    gc_service = (compose.get("services") or {}).get("gc") or {}
    env = gc_service.get("environment") or []
    gc_prefix_entries = [e for e in env if isinstance(e, str) and e.startswith("GC_PREFIXES=")]
    assert gc_prefix_entries, "docker-compose.yml gc service has no GC_PREFIXES environment entry"
    assert "exports/" in gc_prefix_entries[0], (
        f"docker-compose.yml GC_PREFIXES default is missing 'exports/': {gc_prefix_entries[0]}"
    )


# --- export_status format/status CHECK literals ----------------------------


def _check_lists(column: str) -> list:
    statement = _statement(_datasets_area_sql(), "CREATE TABLE export_status (")
    return re.findall(
        rf"CHECK\s*\({column}\s+IN\s+\(([^)]*)\)", statement, flags=re.IGNORECASE
    )


def test_export_status_format_check_matches_canonical_vocabulary():
    """The database's copy of the export_status.format vocabulary must equal
    ('lerobot_v3', 'mcap_passthrough')."""
    matches = _check_lists("format")
    assert len(matches) == 1, "export_status must have one format value list"
    listed = re.findall(r"'([^']*)'", matches[0])
    expected = ["lerobot_v3", "mcap_passthrough"]
    assert listed == expected, f"the baseline lists {listed}, expected {expected}"


def test_export_status_status_check_matches_canonical_vocabulary():
    """The database's copy of the export_status.status vocabulary must equal
    ('pending', 'exporting', 'completed', 'failed', 'dead_letter')."""
    matches = _check_lists("status")
    assert len(matches) == 1, "export_status must have one status value list"
    listed = re.findall(r"'([^']*)'", matches[0])
    expected = ["pending", "exporting", "completed", "failed", "dead_letter"]
    assert listed == expected, f"the baseline lists {listed}, expected {expected}"


def test_export_status_unique_dataset_format_and_fks_cascade():
    sql = _datasets_area_sql()
    assert re.search(r"UNIQUE\s*\(\s*dataset_id\s*,\s*format\s*\)", sql, flags=re.IGNORECASE), (
        "export_status needs UNIQUE (dataset_id, format) -- the exporter's "
        "per-(dataset,format) upsert target"
    )
    assert re.search(
        r"export_status[\s\S]*?dataset_id\s+VARCHAR\(64\)\s+NOT\s+NULL\s+REFERENCES\s+datasets\s*\(\s*dataset_id\s*\)\s+ON\s+DELETE\s+CASCADE",
        sql,
        flags=re.IGNORECASE,
    ), "export_status.dataset_id must FK to datasets(dataset_id) ON DELETE CASCADE"


def test_export_files_key_unique_and_dataset_fk_cascades():
    sql = _datasets_area_sql()
    assert re.search(r"key\s+TEXT\s+UNIQUE\s+NOT\s+NULL", sql, flags=re.IGNORECASE), (
        "export_files.key must be UNIQUE NOT NULL -- it is the join key the "
        "GC reachability source reads"
    )
    assert re.search(
        r"export_files[\s\S]*?dataset_id\s+VARCHAR\(64\)\s+NOT\s+NULL\s+REFERENCES\s+datasets\s*\(\s*dataset_id\s*\)\s+ON\s+DELETE\s+CASCADE",
        sql,
        flags=re.IGNORECASE,
    ), "export_files.dataset_id must FK to datasets(dataset_id) ON DELETE CASCADE"
    assert re.search(
        r"CREATE INDEX \w+ ON export_files\(dataset_id\)",
        sql,
        flags=re.IGNORECASE,
    ), "export_files needs a dataset_id index"


# --- datasets immutability ---------------------------------------------------


def test_datasets_has_before_update_trigger_that_raises():
    """The append-only/content-addressed guarantee must be mechanical: a
    BEFORE UPDATE trigger that unconditionally RAISE EXCEPTIONs, not just a
    comment saying rows are immutable."""
    sql = _datasets_area_sql()
    assert re.search(
        r"CREATE\s+TRIGGER\s+datasets_immutable\s+BEFORE\s+UPDATE\s+ON\s+datasets",
        sql,
        flags=re.IGNORECASE,
    ), "datasets must have a BEFORE UPDATE trigger (datasets_immutable)"
    # The trigger function itself must actually reject the update.
    func_match = re.search(
        r"CREATE\s+FUNCTION\s+datasets_prevent_update\s*\(\s*\)"
        r"[\s\S]*?RAISE\s+EXCEPTION[\s\S]*?\$\$\s+LANGUAGE\s+plpgsql",
        sql,
        flags=re.IGNORECASE,
    )
    assert func_match, (
        "datasets_prevent_update() must RAISE EXCEPTION unconditionally -- "
        "datasets is an append-only, content-addressed registry"
    )


def test_datasets_has_no_before_delete_trigger():
    """Deletion is deliberately left unrestricted (subject only to the FKs)
    so a future GC/retention policy can reap old dataset rows -- immutability
    protects against silent mutation, not against an explicit administrative
    delete."""
    sql = _datasets_area_sql()
    assert not re.search(r"BEFORE\s+DELETE\s+ON\s+datasets", sql, flags=re.IGNORECASE), (
        "datasets must NOT have a BEFORE DELETE trigger -- deletion is "
        "deliberately left available for a future GC/retention policy"
    )


def test_dataset_id_is_a_sha256_shaped_primary_key():
    sql = _datasets_area_sql()
    assert re.search(
        r"CREATE TABLE datasets\s*\(\s*dataset_id\s+VARCHAR\(64\)\s+PRIMARY\s+KEY",
        sql,
        flags=re.IGNORECASE,
    ), "datasets.dataset_id must be VARCHAR(64) PRIMARY KEY -- a sha256 hex digest"
    assert re.search(
        r"manifest\s+JSONB\s+NOT\s+NULL",
        sql,
        flags=re.IGNORECASE,
    ), "datasets.manifest must be JSONB NOT NULL -- the canonical content dataset_id hashes"
    assert re.search(
        r"parent_dataset_id\s+VARCHAR\(64\)\s+REFERENCES\s+datasets\s*\(\s*dataset_id\s*\)",
        sql,
        flags=re.IGNORECASE,
    ), "datasets.parent_dataset_id must self-reference datasets(dataset_id) for version lineage"


# --- dataset_episodes provenance-first RESTRICT -----------------------------


def test_dataset_episodes_episode_fk_is_restrict():
    """Provenance-first: an episode referenced by a cut must not silently
    vanish via an upload-delete cascade (episodes.upload_id cascades from
    uploads) -- the opposite of every other FK pointing at episodes in
    this schema (media_files, qa_reports both cascade)."""
    sql = _datasets_area_sql()
    assert re.search(
        r"episode_id\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+episodes\s*\(\s*episode_id\s*\)\s+ON\s+DELETE\s+RESTRICT",
        sql,
        flags=re.IGNORECASE,
    ), "dataset_episodes.episode_id must FK to episodes(episode_id) ON DELETE RESTRICT"
    assert re.search(
        r"dataset_id\s+VARCHAR\(64\)\s+NOT\s+NULL\s+REFERENCES\s+datasets\s*\(\s*dataset_id\s*\)\s+ON\s+DELETE\s+CASCADE",
        sql,
        flags=re.IGNORECASE,
    ), "dataset_episodes.dataset_id must FK to datasets(dataset_id) ON DELETE CASCADE"
    assert re.search(
        r"UNIQUE\s*\(\s*dataset_id\s*,\s*episode_id\s*\)", sql, flags=re.IGNORECASE
    ), "dataset_episodes needs UNIQUE (dataset_id, episode_id)"
    assert re.search(
        r"CREATE INDEX \w+ ON dataset_episodes\(episode_id\)",
        sql,
        flags=re.IGNORECASE,
    ), "dataset_episodes needs an episode_id index"


# --- dataset_evals append-only -----------------------------------------------


def test_dataset_evals_is_append_only_with_no_trigger():
    """dataset_evals is cut->eval lineage: append-only, same discipline as
    qa_reports -- no updated_at column, no update trigger."""
    sql = _datasets_area_sql()
    evals_start = sql.index("CREATE TABLE dataset_evals")
    evals_body = sql[evals_start:]
    # Slice to just this CREATE TABLE statement (up to its closing ');').
    evals_stmt = evals_body[: evals_body.index(");") + 2]
    assert "updated_at" not in evals_stmt, (
        "dataset_evals must not have an updated_at column -- it is append-only"
    )
    assert "update_dataset_evals_updated_at" not in sql, (
        "dataset_evals is append-only and must not have an updated_at trigger"
    )
    assert re.search(
        r"dataset_id\s+VARCHAR\(64\)\s+NOT\s+NULL\s+REFERENCES\s+datasets\s*\(\s*dataset_id\s*\)\s+ON\s+DELETE\s+CASCADE",
        evals_stmt,
        flags=re.IGNORECASE,
    ), "dataset_evals.dataset_id must FK to datasets(dataset_id) ON DELETE CASCADE"
    assert re.search(
        r"outcome\s+JSONB\s+NOT\s+NULL", evals_stmt, flags=re.IGNORECASE
    ), "dataset_evals.outcome must be JSONB NOT NULL"
    assert re.search(
        r"CREATE INDEX \w+ ON dataset_evals\(dataset_id,\s*created_at DESC\)",
        sql,
        flags=re.IGNORECASE,
    ), "dataset_evals needs a (dataset_id, created_at DESC) index"
