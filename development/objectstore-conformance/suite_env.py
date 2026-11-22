"""Environment of the object-store conformance suite.

Every program of the suite starts with `require_env()`. It exits with status 2 and names the
missing variables (never their values) before any request is sent. All five service keys are
required: a data-path probe never falls back to the root key. The root key only seeds fixtures
and does backup and restore.

`python suite_env.py` is the gate's first step.
"""

from __future__ import annotations

import os
import sys

SERVICES = ("ingest", "processor", "exporter", "query", "gc")
BASE = ("S3_ENDPOINT", "S3_BUCKET", "ROOT_ACCESS_KEY", "ROOT_SECRET_KEY")


def key_names() -> list[str]:
    return [f"{s.upper()}_S3_{kind}" for s in SERVICES for kind in ("ACCESS_KEY", "SECRET_KEY")]


def require_env(extra: tuple[str, ...] = ()) -> None:
    missing = [n for n in (*BASE, *key_names(), *extra) if not os.environ.get(n)]
    if missing:
        print(f"conformance: missing required environment: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def service_creds(service: str) -> tuple[str, str]:
    return os.environ[f"{service.upper()}_S3_ACCESS_KEY"], os.environ[f"{service.upper()}_S3_SECRET_KEY"]


if __name__ == "__main__":
    require_env()
    print("conformance: all five service keys and the root key are set")
