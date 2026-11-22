"""Air-gap-safe `httpfs` install, shared by every place this service opens a
DuckDB connection.

The problem this exists to fix (measured on an air-gapped host): DuckDB's
extension mechanism resolves `INSTALL httpfs` over HTTP by default, against
`extensions.duckdb.org`. That is fine on a developer box with a route out, and
fatal on a genuinely air-gapped one — the query service's own log during that
run was `_duckdb.IOException: IO Error: Failed to download extension "httpfs"
at URL "http://extensions.duckdb.org/..."`.

The fix is a build-time one, not a runtime one: `phases/05-query-service/Dockerfile`
bakes the extension into the image by running `INSTALL httpfs` once, at build,
against `extension_directory=/opt/duckdb-extensions`, while the image build
still has network. At runtime, pointing a fresh connection's
`extension_directory` at that same path makes `INSTALL httpfs` a local
filesystem read that was already satisfied at build time — no network
involved, regardless of whether the box has a route out.

This module is deliberately dependency-free (stdlib + `duckdb` only, no
imports from `query_service`) so both `planner.open_duckdb()` and
`uploads._open_duckdb_for_uploads()` can call it without reintroducing the
import cycle between those two modules that `uploads._open_duckdb_for_uploads`
already documents as the reason it keeps its own connection helper.
"""
from __future__ import annotations

import os

import duckdb

#: Where the Dockerfile bakes the extension, and what it points
#: `DUCKDB_EXTENSION_DIRECTORY` at via `ENV`. Kept as the default here too, so
#: a container that forgets to set the env var still gets the baked path
#: rather than silently falling through to DuckDB's own default (which is
#: keyed to the DuckDB *client* version/OS and would try the network on
#: mismatch).
DEFAULT_EXTENSION_DIRECTORY = "/opt/duckdb-extensions"


def install_httpfs(con: "duckdb.DuckDBPyConnection") -> None:
    """Install and load `httpfs` on `con` without touching the network.

    Reads the baked extension directory from `DUCKDB_EXTENSION_DIRECTORY`
    (default `DEFAULT_EXTENSION_DIRECTORY`, the path the Dockerfile bakes
    into the image) and sets it via `SET extension_directory=...` BEFORE the
    `INSTALL`/`LOAD`, so DuckDB resolves `httpfs` from the local copy the
    image build already fetched instead of reaching for
    `extensions.duckdb.org`.

    A dev environment that runs this service outside the shipped image (no
    baked directory) still works: setting the env var to the empty string
    skips the `SET extension_directory` call entirely and falls back to
    DuckDB's normal (online) install behaviour, exactly as before this
    module existed.
    """
    extension_dir = os.environ.get("DUCKDB_EXTENSION_DIRECTORY", DEFAULT_EXTENSION_DIRECTORY)
    if extension_dir:
        con.execute(f"SET extension_directory='{extension_dir}';")
    con.execute("INSTALL httpfs; LOAD httpfs;")
