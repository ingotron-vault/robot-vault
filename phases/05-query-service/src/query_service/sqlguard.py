"""Read-only gate for the ad-hoc SQL console (`POST /api/v1/query/sql`).

It matters a great deal: the console executes caller-supplied SQL on a
DuckDB connection that holds httpfs plus live S3 credentials, and
without this module that SQL would run completely unvalidated. A
`COPY (SELECT 1) TO 's3://robotics-data/...'` would then WRITE to
the bucket, which would make the console a write path into the object store.

Two layers:

1. A PARSER-BASED relation allowlist (`validate_read_only_sql`) -- the primary
   defence. Rather than lexing the SQL ourselves (a regex masker desyncs from
   DuckDB's real lexer: e-strings, dollar-quotes and hex escapes all slip past
   a hand-rolled one), the statement is handed to DuckDB's OWN parser via
   `json_serialize_sql`, which parses but does NOT execute, on a throwaway
   connection with no S3 context. From the AST we reject anything that is not a
   single top-level SELECT/WITH, any table function at all, and any base table
   that is not `all_parquet` or a CTE in scope AT THAT POINT. A DuckDB
   replacement scan (`FROM 's3://other/x'`, `FROM '/etc/passwd'`) surfaces as a
   base table whose resolved name is that path, so it is caught uniformly and
   the parser cannot desync from the executor.

2. `planner.restrict_connection` limits every query connection, including
   the console's. The connection can open only objects under the processed
   Parquet prefix: no local files, no other bucket or prefix, no HTTP URL.
   It cannot install or auto-load extensions or store persistent secrets.
   Its configuration is locked, so a statement that somehow passed layer 1
   cannot change the S3 credentials or remove these limits.

CTE scoping is deliberately definition-order-aware: a CTE body sees only the
CTEs defined BEFORE it (plus itself if RECURSIVE), which is what DuckDB does.
Over-approximating sibling scope would let a query name a LATER sibling CTE
after a malicious path, so the validator would allow a FROM that DuckDB
resolves as a replacement scan.

`disabled_filesystems` is deliberately not used: it was verified to also break
the console's legitimate S3 parquet reads on this DuckDB build. The
`allowed_directories` plus `enable_external_access=false` pair that
`planner.restrict_connection` uses was verified not to break them.
"""
import json
from typing import Optional, Set, Tuple

import duckdb

_MAX_SQL_LEN = 5000

# The only base relation a console query may read. `run_sql` creates exactly
# one view (`all_parquet`); everything else must be a CTE or a subquery.
_ALLOWED_RELATIONS = frozenset({"all_parquet"})

# Scalar functions that read the session's own configuration.
#
# The relation walk above only inspects relations -- BASE_TABLE and
# TABLE_FUNCTION nodes. A scalar function call in the SELECT list, such as
# `SELECT current_setting('s3_secret_access_key')`, is neither, so the walk
# alone would not stop it. `SET lock_configuration=true` does not help: it
# prevents *writing* settings, not reading them. `duckdb_settings()` is
# rejected by the relation walk because it is a table function.
#
# This is defence in depth. The credentials are not session settings: they
# live in a DuckDB SECRET (see `planner.install_s3_credentials`), so
# `current_setting` returns NULL for them. This denylist makes the attempt a
# clean 400 rather than a successful query returning NULL, and protects a
# connection that sets credentials with SET.
#
# Deliberately a denylist and not an allowlist of permitted functions: the
# console exists to run arbitrary read-only analytics SQL, so an allowlist
# would reject legitimate aggregates, window functions and casts that nobody
# enumerated in advance; the guard must accept registered Parquet keys safely.
# The allowlist that matters is the relation allowlist; what is enumerable
# here is the small set of functions whose whole purpose is introspection.
#
# `getenv` is included because it reads the *process* environment, which is
# exactly where `configs/query.env` places the object-store credentials
# (AWS_SECRET_ACCESS_KEY). The pinned DuckDB (>=1.5,<2) does not currently
# register `getenv` as a scalar function, so `SELECT getenv(...)` fails to
# execute today -- but the pin is a range, a future in-range build could expose
# it, and this denylist is the cheap, durable place to keep that door shut.
_DENIED_FUNCTIONS = frozenset({
    "current_setting",
    "getvariable",
    "getenv",
})


_PARSER_CONFIG = {
    "enable_external_access": False,
    "allow_persistent_secrets": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "lock_configuration": True,
}


def _parse_statements(sql: str) -> Tuple[Optional[dict], Optional[str]]:
    """Parse (NOT execute) `sql` with DuckDB's own parser via
    `json_serialize_sql`, on a throwaway in-memory connection with no S3/view
    context. Returns (parsed_json, error_reason). Fails CLOSED: any exception
    (incl. the parser function being unavailable) yields an error reason so the
    caller rejects the statement."""
    con = None
    try:
        # Parsing needs no files, extensions or secrets. Keep this throwaway
        # connection unable to reach any of them.
        con = duckdb.connect(database=":memory:", config=_PARSER_CONFIG)
        raw = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
        return json.loads(raw), None
    except Exception as e:
        return None, f"could not parse SQL ({type(e).__name__})"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


# A legitimate console relation (the `all_parquet` view or a CTE/alias name) is
# a plain SQL identifier and can NEVER contain a path/URL separator OR a dot.
# Any base table whose resolved name contains one of these is a replacement-scan
# file path / URL / bare filename (a scannable file needs a recognized
# extension -> a dot). Qualified names arrive via schema_name/catalog_name
# (rejected separately), never as a dotted table_name, so `.` has no false
# positives on legitimate relations.
_PATHLIKE_CHARS = ("/", "\\", ":", ".")


def _cte_entries(node: dict):
    """Ordered CTE definitions from this node's `cte_map`: list of
    (name_lower_or_None, entry_dict, is_recursive)."""
    out = []
    cm = node.get("cte_map")
    if isinstance(cm, dict):
        for entry in (cm.get("map") or []):
            key = entry.get("key")
            kl = key.lower() if isinstance(key, str) else None
            val = entry.get("value") if isinstance(entry.get("value"), dict) else {}
            qnode = (val.get("query") or {}).get("node") or {}
            recursive = qnode.get("type") == "RECURSIVE_CTE_NODE"
            out.append((kl, entry, recursive))
    return out


def _check_base_table(node: dict, scope: Set[str]) -> Optional[str]:
    if node.get("schema_name") or node.get("catalog_name"):
        qualified = ".".join(
            x for x in (node.get("catalog_name"), node.get("schema_name"), node.get("table_name")) if x
        )
        return f"qualified relation '{qualified}' is not allowed; the console may only read {sorted(_ALLOWED_RELATIONS)}"
    name = node.get("table_name") or ""
    if any(ch in name for ch in _PATHLIKE_CHARS):
        # A file path / URL / bare filename with an extension.
        return f"relation '{name}' looks like a file path / URL and is not allowed"
    if name.lower() not in (set(_ALLOWED_RELATIONS) | scope):
        return f"relation '{name}' is not allowed; the console may only read {sorted(set(_ALLOWED_RELATIONS) | scope)}"
    return None


def _walk_relations(obj, scope: Set[str]) -> Optional[str]:
    """SCOPE-AWARE, definition-order-aware, top-down AST walk. `scope` is the
    set of CTE names lexically visible at `obj`. Returns a rejection reason, or
    None.

    Every relation is validated against {all_parquet} plus ONLY the CTEs in
    scope where it appears. CTE scoping matches DuckDB exactly: a CTE body sees
    the CTEs defined BEFORE it in the same WITH (plus itself only if RECURSIVE),
    while the WITH's own query body sees all of them. Over-approximating sibling
    scope (adding every sibling everywhere) would let an attacker name a
    later sibling CTE after the malicious path so the validator whitelisted a
    FROM that DuckDB actually resolves as a replacement scan. Table functions,
    schema/catalog-qualified relations, and path/URL/dotted base-table names are
    rejected outright."""
    if isinstance(obj, dict):
        t = obj.get("type")
        # Defence in depth: a relation target can be carried as a bare
        # `table_name` string on node types other than BASE_TABLE. DuckDB
        # serialises SUMMARIZE/DESCRIBE/SHOW as a SELECT_NODE wrapping a
        # SHOW_REF whose `table_name` holds the target; only BASE_TABLE reaches
        # `_check_base_table`, so a path/URL carried on any other node would
        # otherwise slip past the file-path guard. A legitimate console
        # relation (the `all_parquet` view or a CTE/alias) never contains a
        # path separator or a dot, so screening every `table_name` we encounter
        # for `_PATHLIKE_CHARS` has no false positives on real analytics SQL.
        tn = obj.get("table_name")
        if isinstance(tn, str) and any(ch in tn for ch in _PATHLIKE_CHARS):
            return f"relation '{tn}' looks like a file path / URL and is not allowed"
        if t == "TABLE_FUNCTION":
            fn = (obj.get("function") or {}).get("function_name")
            return (
                f"table function '{fn}' is not allowed; "
                f"the console may only read {sorted(_ALLOWED_RELATIONS)}"
            )
        # Scalar function calls live in expression position, not
        # relation position, so they reach here as FUNCTION nodes rather than
        # TABLE_FUNCTION ones. See _DENIED_FUNCTIONS.
        if obj.get("class") == "FUNCTION":
            fname = obj.get("function_name")
            if isinstance(fname, str) and fname.lower() in _DENIED_FUNCTIONS:
                return (
                    f"function '{fname}' is not allowed; it reads the "
                    f"session's own configuration"
                )
        if t == "BASE_TABLE":
            r = _check_base_table(obj, scope)
            if r is not None:
                return r

        entries = _cte_entries(obj)
        all_keys = {kl for (kl, _e, _r) in entries if kl}
        # The WITH's own query body may reference every CTE it defines.
        body_scope = scope | all_keys

        # Each CTE definition sees only EARLIER siblings (+ itself if RECURSIVE),
        # matching DuckDB -- NOT all siblings.
        earlier: Set[str] = set()
        for kl, entry, recursive in entries:
            def_scope = scope | earlier
            if recursive and kl:
                def_scope = def_scope | {kl}
            r = _walk_relations(entry.get("value"), def_scope)
            if r is not None:
                return r
            if kl:
                earlier.add(kl)

        for k, v in obj.items():
            if k == "cte_map":
                continue
            r = _walk_relations(v, body_scope)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_relations(v, scope)
            if r is not None:
                return r
    return None


def validate_read_only_sql(sql: str) -> Optional[str]:
    """Return a human-readable rejection reason, or None if the statement is a
    single read-only SELECT/WITH that reads only allowlisted relations. Uses
    DuckDB's parser (see module docstring) -- not a regex lexer."""
    if not sql or not sql.strip():
        return "empty statement"

    parsed, err = _parse_statements(sql)
    if err is not None:
        return err
    if not isinstance(parsed, dict) or parsed.get("error"):
        # json_serialize_sql only serializes SELECT statements: COPY/INSERT/
        # UPDATE/DELETE/PRAGMA/SET/ATTACH/CALL/DDL and genuine parse errors all
        # come back error=true -> rejected by construction, no keyword matching.
        return "only read-only SELECT / WITH statements are allowed"

    stmts = parsed.get("statements")
    if not isinstance(stmts, list) or len(stmts) != 1:
        return "only a single statement is allowed"

    node = (stmts[0] or {}).get("node") or {}
    if node.get("type") not in ("SELECT_NODE", "SET_OPERATION_NODE"):
        return "only read-only SELECT / WITH statements are allowed"

    # Scope-aware relation allowlist over the whole statement. A replacement
    # scan surfaces as a BASE_TABLE whose resolved name is the path/URL, and is
    # rejected both by the path-char check and by not being an in-scope
    # relation; table functions are rejected wholesale.
    return _walk_relations(parsed["statements"][0], set())
