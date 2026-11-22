#!/usr/bin/env python3
"""Wait until every service in a release edition is actually ready.

Unlike a query-only curl loop, this checks the health state of every long-lived
container and the successful exit state of all three initialization jobs.  It uses
Compose only to resolve project-scoped container IDs; Docker's inspect result
is the source of truth for runtime state.

Then it checks what the running services say they are:
  - query: GET /api/v1/edition (edition, core_version, adapter_api, pro_version);
  - processor and (Pro) exporter: the one `{"rv_identity": ...}` JSON line each
    logs at startup, read from the running container's log;
  - the served UI: `<meta name="rv-build" content="bundle=<edition> core=<v> pro=<v|none>">`
    in /ui/.
All of them must report the requested edition and the same core_version,
pro_version and adapter_api. Any difference (a mixed image set, a core UI
bundle in a Pro query image, a service started in the wrong edition) fails.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


COMMON_READY_SERVICES: Tuple[str, ...] = (
    "postgres",
    "objectstore",
    "redis",
    "ingest",
    "processor",
    "query",
    "gc",
)
COMPLETED_SERVICES: Tuple[str, ...] = (
    "db-migrate",
    "postgres-roles-ready",
    "objectstore-access-ready",
)
COMMAND_TIMEOUT_SECONDS = 10.0


def _run(command: Sequence[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout


def _compose_command(args: argparse.Namespace) -> List[str]:
    command = ["docker", "compose", "-p", args.project, "--env-file", str(args.env_file)]
    for compose_file in args.compose_file:
        command.extend(("-f", str(compose_file)))
    return command


def _container_ids(compose: Sequence[str], service: str) -> List[str]:
    output = _run((*compose, "ps", "--all", "--quiet", service))
    return [line.strip() for line in output.splitlines() if line.strip()]


def _inspect(container_id: str) -> Mapping[str, object]:
    output = _run(("docker", "inspect", container_id))
    document = json.loads(output)
    if not isinstance(document, list) or len(document) != 1:
        raise RuntimeError(f"unexpected docker inspect result for {container_id}")
    item = document[0]
    if not isinstance(item, dict):
        raise RuntimeError(f"unexpected docker inspect result for {container_id}")
    return item


def _service_state(
    compose: Sequence[str], service: str, *, completed: bool
) -> Tuple[bool, str]:
    ids = _container_ids(compose, service)
    if not ids:
        return False, "missing"

    states: List[str] = []
    all_ready = True
    for container_id in ids:
        document = _inspect(container_id)
        state = document.get("State")
        if not isinstance(state, dict):
            return False, "invalid inspect state"
        status = str(state.get("Status") or "unknown")
        if completed:
            exit_code = state.get("ExitCode")
            ready = status == "exited" and exit_code == 0
            description = f"{status}/exit={exit_code}"
        else:
            health = state.get("Health")
            health_status = (
                str(health.get("Status") or "unknown")
                if isinstance(health, dict)
                else "missing-healthcheck"
            )
            ready = status == "running" and health_status == "healthy"
            description = f"{status}/{health_status}"
        all_ready = all_ready and ready
        states.append(description)
    return all_ready, ",".join(states)


def _required_services(edition: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    ready = COMMON_READY_SERVICES + (("exporter",) if edition == "pro" else ())
    return ready, COMPLETED_SERVICES


IDENTITY_KEYS = ("edition", "core_version", "adapter_api", "pro_version")
_BUILD_META = re.compile(r'<meta name="rv-build" content="([^"]*)"')
# Runs inside the query container: the API identity and the served UI build.
_QUERY_PROBE = (
    "import json, re, ssl, urllib.request\n"
    "ctx = ssl._create_unverified_context()\n"
    "get = lambda p: urllib.request.urlopen('https://127.0.0.1:8003' + p, timeout=5, context=ctx).read().decode()\n"
    "print(json.dumps({'api': json.loads(get('/api/v1/edition')), 'ui': get('/ui/')}))\n"
)


def parse_identity_line(log: str) -> Optional[Dict[str, Any]]:
    """The last `{"rv_identity": ...}` line of a service log, or None."""

    found = None
    for line in log.splitlines():
        line = line.strip()
        if line.startswith('{"rv_identity"'):
            try:
                found = json.loads(line)["rv_identity"]
            except (ValueError, KeyError, TypeError):
                continue
    return found


def parse_build_meta(html: str) -> Optional[Dict[str, Any]]:
    """`bundle=pro core=0.11.0 pro=0.11.0` from the served index.html, or None."""

    match = _BUILD_META.search(html)
    if not match:
        return None
    fields = dict(item.split("=", 1) for item in match.group(1).split() if "=" in item)
    pro = fields.get("pro")
    return {
        "edition": fields.get("bundle"),
        "core_version": fields.get("core"),
        "pro_version": None if pro in (None, "none") else pro,
    }


def identity_mismatches(expected_edition: str, reports: Mapping[str, Optional[Mapping[str, Any]]]) -> List[str]:
    """Every way the reported identities disagree with each other or the request.

    `reports` maps a source ("query api", "processor", "exporter", "query ui")
    to what it reported; None means it reported nothing. The UI reports no
    adapter_api, so that key is compared only where present.
    """

    problems: List[str] = []
    for source, report in reports.items():
        if report is None:
            problems.append(f"{source}: no identity reported")
        elif report.get("edition") != expected_edition:
            problems.append(f"{source}: edition {report.get('edition')!r}, expected {expected_edition!r}")
    present = {s: r for s, r in reports.items() if r is not None}
    for key in IDENTITY_KEYS[1:]:
        values = {s: r.get(key) for s, r in present.items() if key in r}
        if len(set(map(json.dumps, values.values()))) > 1:
            problems.append(f"{key} differs: " + ", ".join(f"{s}={v!r}" for s, v in values.items()))
    if expected_edition == "core":
        for source, report in present.items():
            if report.get("pro_version") is not None:
                problems.append(f"{source}: reports Pro {report['pro_version']!r} in the core edition")
    return problems


def collect_identities(compose: Sequence[str], edition: str) -> Dict[str, Optional[Dict[str, Any]]]:
    reports: Dict[str, Optional[Dict[str, Any]]] = {}
    probe = json.loads(_run((*compose, "exec", "-T", "query", "python", "-c", _QUERY_PROBE)))
    api = probe.get("api") or {}
    reports["query api"] = {k: api.get(k) for k in IDENTITY_KEYS} if "edition" in api else None
    reports["query ui"] = parse_build_meta(probe.get("ui") or "")
    for service in ("processor", "exporter") if edition == "pro" else ("processor",):
        log = subprocess.run(
            (*compose, "logs", "--no-log-prefix", service),
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        ).stdout
        reports[service] = parse_identity_line(log)
    return reports


def check_identity(args: argparse.Namespace) -> bool:
    reports = collect_identities(_compose_command(args), args.edition)
    problems = identity_mismatches(args.edition, reports)
    print("release identity: " + json.dumps(reports, sort_keys=True), flush=True)
    for problem in problems:
        print(f"release identity MISMATCH: {problem}", file=sys.stderr)
    return not problems


def wait_until_ready(args: argparse.Namespace) -> bool:
    compose = _compose_command(args)
    ready_services, completed_services = _required_services(args.edition)
    if args.skip_init_jobs:
        completed_services = ()
    deadline = time.monotonic() + args.timeout
    last_snapshot: Dict[str, str] = {}

    while True:
        snapshot: Dict[str, str] = {}
        all_ready = True
        try:
            for service in ready_services:
                ready, state = _service_state(compose, service, completed=False)
                snapshot[service] = state
                all_ready = all_ready and ready
            for service in completed_services:
                ready, state = _service_state(compose, service, completed=True)
                snapshot[service] = state
                all_ready = all_ready and ready
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError,
            RuntimeError,
        ) as exc:
            all_ready = False
            snapshot["inspection"] = str(exc)

        if snapshot != last_snapshot:
            summary = ", ".join(f"{name}={state}" for name, state in snapshot.items())
            print(f"release readiness: {summary}", flush=True)
            last_snapshot = snapshot
        if all_ready:
            return True
        if time.monotonic() >= deadline:
            print(
                f"release readiness timed out after {args.timeout:g}s",
                file=sys.stderr,
            )
            subprocess.run((*compose, "ps", "--all"), check=False)
            return False
        time.sleep(args.interval)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--compose-file", required=True, type=Path, action="append",
                        help="repeat for each file of the deployment, in order")
    parser.add_argument("--edition", required=True, choices=("core", "pro"))
    parser.add_argument("--skip-init-jobs", action="store_true",
                        help="check a running deployment: release-up removes the one-shot init "
                             "containers after success, so do not wait for their exit state")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.interval <= 0:
        parser.error("--timeout and --interval must be positive")
    return args


def main() -> int:
    args = parse_args()
    if not wait_until_ready(args):
        return 1
    try:
        return 0 if check_identity(args) else 1
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"release identity check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
