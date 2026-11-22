#!/usr/bin/env python3
"""Refuse a release `up` that would recreate a data volume.

Compose recreates a named volume, and deletes its data, when the volume exists
but its configuration (labels, driver, driver options) differs from the compose
files. It asks "Recreate (data will be lost)?" first. release-up never answers
that prompt (stdin is /dev/null); this check refuses before it, with a reason,
before anything changes.

  docker compose ... config --format json | volume_guard.py --project <project>

The rendered config comes on stdin because it holds secrets; it is never
stored. The output names volumes and the fields or keys that differ, never
their values (driver options can hold credentials).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, List, Mapping


def differences(declared: Mapping[str, Any], inspected: Mapping[str, Any]) -> List[str]:
    """What differs between an existing volume and its declaration, by name only."""

    found: List[str] = []
    have = {k: v for k, v in (inspected.get("Labels") or {}).items()
            if not k.startswith("com.docker.compose.")}  # compose's own bookkeeping
    want = declared.get("labels") or {}
    found += [f"label {k}" for k in sorted(set(have) | set(want)) if have.get(k) != want.get(k)]
    if (inspected.get("Driver") or "local") != (declared.get("driver") or "local"):
        found.append("driver")
    have_opts = inspected.get("Options") or {}
    want_opts = declared.get("driver_opts") or {}
    found += [f"driver option {k}" for k in sorted(set(have_opts) | set(want_opts))
              if have_opts.get(k) != want_opts.get(k)]
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        config = json.load(sys.stdin)
    except ValueError:
        print("volume guard: no rendered compose config on stdin", file=sys.stderr)
        return 1
    problems: List[str] = []
    for key, volume in sorted((config.get("volumes") or {}).items()):
        if volume.get("external"):
            continue
        name = volume.get("name") or f"{args.project}_{key}"
        result = subprocess.run(("docker", "volume", "inspect", name),
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            if "no such volume" in result.stderr.lower():
                continue  # first deployment: compose creates it
            print(f"volume guard: cannot inspect volume {name}: {result.stderr.strip()}", file=sys.stderr)
            return 1
        diff = differences(volume, json.loads(result.stdout)[0])
        if diff:
            problems.append(f"  {name}: {', '.join(diff)}")
    if problems:
        print("REFUSING: existing data volumes differ from the release configuration, so compose "
              "would recreate them and delete their data:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print("Nothing was changed. Restore the previous configuration, or back up the volume "
              "and migrate it by hand.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
