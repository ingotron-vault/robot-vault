from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
import seed_examples  # noqa: E402


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    root.mkdir()
    bags = []
    for names in seed_examples.ROBOT_BAGS.values():
        for name in names:
            payload = (name + "\n").encode()
            (root / name).write_bytes(payload)
            bags.append(
                {
                    "file": name,
                    "sha256": digest(payload),
                    "bytes": len(payload),
                    "label": name.removesuffix(".mcap"),
                }
            )
    (root / "manifest.json").write_text(
        json.dumps({"schema": "robot-vault-example-bundle/v1", "bags": bags})
    )
    return root


def test_prepare_is_idempotent_and_never_replaces_existing_bytes(tmp_path):
    source = bundle(tmp_path)
    entries = seed_examples.load_bundle(source)
    state = tmp_path / "state"
    root, first = seed_examples.prepare_robot_root(
        bundle=source,
        state_dir=state,
        robot_id="example-wheeled-01",
        entries=entries,
    )
    inode = (root / "wheeled-nominal.mcap").stat().st_ino
    root2, second = seed_examples.prepare_robot_root(
        bundle=source,
        state_dir=state,
        robot_id="example-wheeled-01",
        entries=entries,
    )
    assert root2 == root
    assert set(first.values()) == {"copied"}
    assert set(second.values()) == {"reused"}
    assert (root / "wheeled-nominal.mcap").stat().st_ino == inode


def test_prepare_refuses_to_overwrite_a_changed_seed_file(tmp_path):
    source = bundle(tmp_path)
    entries = seed_examples.load_bundle(source)
    state = tmp_path / "state"
    root, _ = seed_examples.prepare_robot_root(
        bundle=source,
        state_dir=state,
        robot_id="example-manipulator-01",
        entries=entries,
    )
    (root / "manipulator-pickplace.mcap").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        seed_examples.prepare_robot_root(
            bundle=source,
            state_dir=state,
            robot_id="example-manipulator-01",
            entries=entries,
        )


def test_generated_config_sets_identity_root_capture_and_owner_only_mode(tmp_path):
    template = tmp_path / "vault.json"
    template.write_text(
        json.dumps(
            {
                "paths": {"robot_root": "/old"},
                "server": {"host": "ingest", "port": 50051},
                "tls": {"enabled": False},
            }
        )
    )
    root = tmp_path / "state" / "robots" / "example-wheeled-01"
    config = seed_examples.build_config(
        robot_id="example-wheeled-01",
        root=root,
        state_dir=tmp_path / "state",
        template=template,
        specific=None,
    )
    value = json.loads(config.read_text())
    assert value["robot_id"] == "example-wheeled-01"
    assert value["paths"]["robot_root"] == str(root)
    assert value["capture"]["permission_ref"] == "public-example-data-v1"
    assert config.stat().st_mode & 0o777 == 0o600
