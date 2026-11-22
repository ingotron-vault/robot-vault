#!/usr/bin/env python3
"""Idempotently enroll and upload Robot Vault's four public examples.

There is deliberately no database shortcut here. Robot identities go through
ingest's authenticated ``/admin/robots`` owner surface and every MCAP goes
through the same ``rsm scan`` -> ``rsm hash`` -> ``rua upload`` path as a real
robot. Durable local manifests make re-runs no-ops; ingest's deterministic
upload identity is the second idempotency boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence

ROBOT_BAGS = {
    "example-wheeled-01": (
        "wheeled-nominal.mcap",
        "imperfect-run.mcap",
        "pii-bearing.mcap",
    ),
    "example-manipulator-01": ("manipulator-pickplace.mcap",),
}
ROBOT_NAMES = {
    "example-wheeled-01": "Robot Vault Examples — Wheeled",
    "example-manipulator-01": "Robot Vault Examples — Manipulator",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bundle(bundle: Path) -> Mapping[str, Mapping[str, object]]:
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != "robot-vault-example-bundle/v1":
        raise RuntimeError(f"unsupported example manifest: {manifest.get('schema')!r}")
    entries = {entry["file"]: entry for entry in manifest.get("bags", [])}
    expected = {name for names in ROBOT_BAGS.values() for name in names}
    if set(entries) != expected:
        raise RuntimeError(
            f"bundle inventory mismatch: expected {sorted(expected)}, got {sorted(entries)}"
        )
    for name, entry in entries.items():
        path = bundle / name
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = _sha256(path)
        if actual != entry.get("sha256"):
            raise RuntimeError(
                f"bundle digest mismatch for {name}: expected {entry.get('sha256')}, got {actual}"
            )
    return entries


def _copy_once(source: Path, destination: Path, expected_sha256: str) -> str:
    if destination.exists():
        if not destination.is_file():
            raise RuntimeError(f"seed destination is not a regular file: {destination}")
        actual = _sha256(destination)
        if actual != expected_sha256:
            raise RuntimeError(
                f"refusing to overwrite changed seed file {destination}: "
                f"expected {expected_sha256}, got {actual}"
            )
        return "reused"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    actual = _sha256(destination)
    if actual != expected_sha256:
        destination.unlink()
        raise RuntimeError(f"copy verification failed for {destination}")
    stable = time.time() - 3600
    os.utime(destination, (stable, stable))
    return "copied"


def prepare_robot_root(
    *,
    bundle: Path,
    state_dir: Path,
    robot_id: str,
    entries: Mapping[str, Mapping[str, object]],
) -> tuple[Path, Dict[str, str]]:
    root = (state_dir / "robots" / robot_id).resolve()
    outcomes = {}
    for name in ROBOT_BAGS[robot_id]:
        outcomes[name] = _copy_once(
            bundle / name,
            root / name,
            str(entries[name]["sha256"]),
        )
    return root, outcomes


def _parse_configs(values: Iterable[str]) -> Dict[str, Path]:
    result: Dict[str, Path] = {}
    for raw in values:
        robot_id, separator, path = raw.partition("=")
        if not separator or robot_id not in ROBOT_BAGS or not path:
            raise ValueError(
                "--config must be ROBOT_ID=/path/to/vault.json for one of "
                + ", ".join(ROBOT_BAGS)
            )
        result[robot_id] = Path(path)
    return result


def build_config(
    *,
    robot_id: str,
    root: Path,
    state_dir: Path,
    template: Optional[Path],
    specific: Optional[Path],
) -> Path:
    source = specific or template
    if source is None:
        raise RuntimeError(f"no config or config template supplied for {robot_id}")
    config = json.loads(source.read_text())
    config["robot_id"] = robot_id
    config.setdefault("paths", {})["robot_root"] = str(root)
    config["paths"]["extension"] = ".mcap"
    config["capture"] = {
        "operator": "robot-vault-example-seeder",
        "site": "example-data",
        "permission_ref": "public-example-data-v1",
        "session_tags": "example-data,public,alpha",
        "task_label": "see per-bag /session/task marker",
    }
    config.setdefault("upload_agent", {})["min_stable_age_seconds"] = 5
    config_dir = state_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{robot_id}.json"
    # The generated config can contain private-key paths but never a token.
    # Write atomically and keep it owner-only for parity with robot configs.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def _register_via_container(container: str, robot_id: str) -> Mapping[str, object]:
    query = urllib.parse.urlencode(
        {
            "robot_id": robot_id,
            "name": ROBOT_NAMES[robot_id],
            "metadata": json.dumps(
                {
                    "source": "Robot Vault public example-data bundle",
                    "example_data": True,
                    "example_data_version": 1,
                },
                separators=(",", ":"),
            ),
        }
    )
    url = f"http://localhost:8080/admin/robots?{query}"
    completed = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            'curl -fsS -X POST -H "authorization: Bearer $INGEST_AUTH_TOKEN" "$1"',
            "sh",
            url,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    print(completed.stdout.strip())
    return json.loads(completed.stdout)


def _register_via_url(url: str, robot_id: str) -> Mapping[str, object]:
    endpoint = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(
        {
            "robot_id": robot_id,
            "name": ROBOT_NAMES[robot_id],
            "metadata": json.dumps(
                {
                    "source": "Robot Vault public example-data bundle",
                    "example_data": True,
                    "example_data_version": 1,
                },
                separators=(",", ":"),
            ),
        }
    )
    request = urllib.request.Request(endpoint, method="POST")
    token = os.environ.get("INGEST_AUTH_TOKEN", "").strip()
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        raise RuntimeError(f"robot enrollment failed ({error.code}): {body}") from error
    print(body)
    return json.loads(body)


def register_robot(
    *, robot_id: str, admin_url: Optional[str], admin_container: Optional[str]
) -> Mapping[str, object]:
    if admin_container:
        return _register_via_container(admin_container, robot_id)
    if admin_url:
        return _register_via_url(admin_url, robot_id)
    raise RuntimeError("one of --admin-container or --admin-url is required")


def _run(command: Sequence[str]) -> str:
    print("+ " + " ".join(command))
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return completed.stdout


def upload_robot(*, robot_id: str, root: Path, config: Path) -> Mapping[str, object]:
    _run(("rua", "config", "validate", "--config", str(config)))
    _run(("rsm", "scan", "--config", str(config), "--root", str(root)))
    _run(("rsm", "hash", "--config", str(config), "--root", str(root)))
    uploaded = []
    skipped = []
    # One file per invocation is rua's normal CLI contract. An extra terminal
    # invocation proves the pending queue is empty and makes a rerun visibly a
    # no-op rather than relying on a guessed number of files.
    for _ in range(len(ROBOT_BAGS[robot_id]) + 1):
        raw = _run(("rua", "upload", "--config", str(config), "--root", str(root)))
        result = json.loads(raw)
        uploaded_value = result.get("uploaded")
        skipped.extend(result.get("skipped") or [])
        if not uploaded_value:
            break
        uploaded.append(uploaded_value)
    else:
        raise RuntimeError(f"rua queue for {robot_id} did not drain")
    return {"uploaded": uploaded, "skipped": skipped}


def seed_examples(args) -> Mapping[str, object]:
    bundle = Path(args.bundle).resolve()
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    entries = load_bundle(bundle)
    configs = _parse_configs(args.config)
    if configs and set(configs) != set(ROBOT_BAGS):
        missing = sorted(set(ROBOT_BAGS) - set(configs))
        raise RuntimeError(f"specific configs must cover both robots; missing {missing}")

    summary = {"schema": "robot-vault-example-seed/v1", "robots": {}}
    for robot_id in ROBOT_BAGS:
        root, copy_outcomes = prepare_robot_root(
            bundle=bundle,
            state_dir=state_dir,
            robot_id=robot_id,
            entries=entries,
        )
        config = build_config(
            robot_id=robot_id,
            root=root,
            state_dir=state_dir,
            template=Path(args.config_template) if args.config_template else None,
            specific=configs.get(robot_id),
        )
        enrollment = register_robot(
            robot_id=robot_id,
            admin_url=args.admin_url,
            admin_container=args.admin_container,
        )
        upload = upload_robot(robot_id=robot_id, root=root, config=config)
        summary["robots"][robot_id] = {
            "root": str(root),
            "config": str(config),
            "files": copy_outcomes,
            "enrollment": enrollment,
            **upload,
        }
    summary_path = state_dir / "last-seed-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default="examples")
    parser.add_argument("--state-dir", default=".example-seed")
    parser.add_argument(
        "--config-template",
        default="configs/vault.local.json",
        help="base config for both identities (ignored per robot when --config is used)",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="ROBOT_ID=PATH",
        help="per-identity config, repeat once for each example robot",
    )
    admin = parser.add_mutually_exclusive_group(required=True)
    admin.add_argument(
        "--admin-container",
        help="ingest container; the bearer stays in its environment (acceptance path)",
    )
    admin.add_argument("--admin-url", help="reachable ingest /admin/robots URL")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    seed_examples(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
