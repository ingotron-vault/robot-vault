"""There is exactly ONE garbage collector in this repo.

A second, duplicated GC (for example behind `/api/admin/gc/dry-run` and
`/api/admin/gc/execute`) would drift from `tools/s3_gc.py`. If it built its
reachable set with `LIKE 's3://%'` against `processed_topics.parquet_path`,
which the processor writes scheme-less, the query would match nothing, the
reachable set for `processed/parquet/` would be empty, and executing it
would sweep every live Parquet file in the lake.

These are pure source/AST invariants: no Postgres, no S3, no network. They
live in a phase unit suite (not tests/integration, which no CI gate runs and
which skips wholesale when psycopg/boto3 are absent) precisely because a
guard that can silently not-run is not a guard. `make test-unit` and
the CI workflow both execute this file.
"""
import ast
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

GC_MODULE = "tools/s3_gc.py"

# Directories with no first-party source in them.
_SCAN_EXCLUDED_DIRS = {".git", ".claude", "node_modules", ".venv", "venv", "__pycache__", "data", "target"}

# Tests legitimately delete S3 objects (their own fixtures' scratch keys) and
# quote the buggy SQL when describing it, so they are not candidate GCs.
# Test code is excluded wherever it lives: a test may legitimately delete its
# own S3 fixtures, and these very tests quote the buggy SQL when describing it.
# Matched on any `tests` path component, not a top-level `tests/` prefix --
# phase suites live at phases/*/tests/, including this file.
_SCAN_EXCLUDED_PARTS = {"tests"}

# Benchmark teardown is not a garbage collector. The distinction this file
# actually protects is DERIVED reachability: a GC decides what is unreferenced
# and sweeps it. development/bench/bench_cleanup.py deletes only the objects
# of one explicitly-named bench robot passed on argv (and refuses ids that do
# not look like bench robots), exactly as tests/e2e/test_pipeline_e2e.py::_cleanup
# does for its own fixtures. It derives nothing and sweeps nothing.
# development/objectstore-conformance/ is the object-store gate: it runs only against its own
# throwaway store. iam_probe.py probes each service key's delete permission on its own probe keys;
# conformance.py and rustfs_contract_probe.py delete the keys they created to prove the gc key's
# DeleteObjects and the ingest key's guarded delete.
# tools/robot_vault_backup.py: restore is not a GC. It runs only with the services stopped, after a
# full preflight of a checksummed manifest, and only after every manifest object is uploaded and
# verified does it delete the keys absent from that same recovery point, so the bucket equals the
# backup whose database dump is restored with it. It derives nothing from live references.
# development/backup-restore-e2e/probe.py: the backup/restore E2E probe; e2e.py runs it against the
# throwaway stack that run.sh starts; run.sh refuses an unset COMPOSE_PROJECT_NAME and any project
# that already has containers or volumes.
_SCAN_EXCLUDED_FILES = {
    "tools/robot_vault_backup.py",
    "development/backup-restore-e2e/probe.py",
    "development/bench/bench_cleanup.py",
    "development/objectstore-conformance/iam_probe.py",
    "development/objectstore-conformance/conformance.py",
    "development/objectstore-conformance/rustfs_contract_probe.py",
}

# A throwaway-stack test tool outside this list can opt out with this exact line
# in its source. Use it only for a tool that deletes objects of its own test stack.
_NOT_A_GC_MARKER = "# single-gc-invariant: not a collector (throwaway test stack only)"


def _first_party_python_files():
    """Every first-party .py file in the repo, as (repo-relative path, AST).

    AST rather than raw text on purpose: `#` comments in this file quote the
    broken SQL, and a substring scan would match them and fail for the wrong
    reason. Only code the interpreter actually runs should count.
    """
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if any(part in _SCAN_EXCLUDED_DIRS for part in rel.parts):
            continue
        rel_str = rel.as_posix()
        if rel_str == GC_MODULE or rel_str in _SCAN_EXCLUDED_FILES or (_SCAN_EXCLUDED_PARTS & set(rel.parts)):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            # Generated stubs / non-UTF8 files are not hand-written GCs.
            continue
        if _NOT_A_GC_MARKER in source.splitlines():
            continue
        yield rel_str, tree


def _called_attribute_names(tree):
    """Names of every attribute call in `tree` (i.e. the `f` in `x.f(...)`)."""
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _string_constants(tree):
    """Every string literal in `tree` (docstrings included -- see callers)."""
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_no_second_gc_implementation_deletes_s3_objects():
    """Only tools/s3_gc.py may delete S3 objects.

    Fails if any other module calls `delete_object` or `delete_objects`.
    If this fails for a NEW module, the fix is not to add it to an allowlist:
    route the deletion through tools/s3_gc.py so it inherits
    `reachable_keys_from_pg` + the `sweep_prefix` empty-set guard.
    """
    offenders = [
        rel
        for rel, tree in _first_party_python_files()
        if {"delete_object", "delete_objects"} & _called_attribute_names(tree)
    ]
    assert offenders == [], (
        "S3 object deletion outside " + GC_MODULE + ": " + ", ".join(offenders) + ". "
        "A second sweeper cannot inherit the reachability fix or the empty-set guard "
        "and will drift out of sync; one wrong reachable set deletes the whole lake."
    )


def test_no_second_gc_implementation_derives_reachability():
    """Only tools/s3_gc.py may derive the reachable set from Postgres.

    Catches the specific broken derivation (`LIKE 's3://%'`, which the
    processor's scheme-less `parquet_path` never matches) wherever it is
    executed, plus any other query that reads `storage_path`/`parquet_path`
    to decide what is reachable.
    """
    offenders = []
    for rel, tree in _first_party_python_files():
        for literal in _string_constants(tree):
            lowered = " ".join(literal.lower().split())
            if "like 's3://%'" in lowered and ("storage_path" in lowered or "parquet_path" in lowered):
                offenders.append(rel)
                break
    assert offenders == [], (
        "reachable-set derivation outside " + GC_MODULE + ": " + ", ".join(offenders) + ". "
        "`LIKE 's3://%'` never matches processed_topics.parquet_path (the processor "
        "writes it scheme-less), so such a query yields an empty reachable set and "
        "marks every live object collectable. Use s3_gc.reachable_keys_from_pg."
    )


def test_query_service_exposes_no_gc_endpoint():
    """The read path must expose no HTTP trigger for garbage collection.

    Complements the two source scans above at the surface that actually
    matters: even a *correct* GC behind an admin POST would be a second
    delete path with a caller-supplied free-text `prefix` and none of the
    `gc` service's dry-run gating or metrics. Fails if any route path of the
    query service contains `/gc`.
    """
    src = REPO_ROOT / "phases" / "05-query-service" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    # main.py mounts StaticFiles over phases/05-query-service/src/ui, which
    # only exists in the query service image (the ui-spa npm build), so a
    # bare import raises in the dev/test image. Stub it for the import only
    # -- same workaround as the query-service unit tests.
    import fastapi.staticfiles as fastapi_staticfiles

    original_static_files = fastapi_staticfiles.StaticFiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    if "query_service.main" in sys.modules:
        main = sys.modules["query_service.main"]
    else:
        fastapi_staticfiles.StaticFiles = _StubStaticFiles
        try:
            main = importlib.import_module("query_service.main")
        finally:
            fastapi_staticfiles.StaticFiles = original_static_files

    gc_routes = sorted(
        getattr(r, "path", "")
        for r in main.app.routes
        if "/gc" in getattr(r, "path", "")
    )
    assert gc_routes == [], f"query service exposes GC endpoints: {gc_routes}"
