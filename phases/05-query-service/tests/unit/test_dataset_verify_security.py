"""Adversarial tests for the standalone offline dataset verifier.

These use the CLI as a subprocess because graceful exit codes and the absence
of tracebacks are part of the customer-facing contract.  The test imports no
query-service runtime code; `tools/dataset_verify.py` must remain independently
copyable to an air-gapped machine.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
VERIFY_CLI = REPO_ROOT / "tools" / "dataset_verify.py"


def _manifest() -> dict:
    return {
        "schema": "robot-vault-dataset-manifest/v2",
        "name": "security-test",
        "filter_spec": {},
        "parent_dataset_id": None,
        "created_at_utc": "2026-08-09T00:00:00Z",
        "episodes": [
            {
                "episode_id": "11111111-1111-1111-1111-111111111111",
                "upload_id": "22222222-2222-2222-2222-222222222222",
                "robot_id": "sim-security",
                "t_start_ns": 1,
                "t_end_ns": 2,
                "n_messages": 1,
                "task_label": None,
                "source_mcap_sha256": "a" * 64,
                "media": [
                    {
                        "key": "processed/media/chunk-000.mp4",
                        "sha256": hashlib.sha256(b"media").hexdigest(),
                        "codec": "av1",
                        "n_frames": 1,
                        "pii_status": "scrubbed",
                    }
                ],
                "parquet": [
                    {
                        "file_path": "robotics-data/processed/parquet/part-000.parquet",
                        "row_count": 1,
                        "file_size_bytes": len(b"parquet"),
                        "schema_version": 1,
                        "sha256": hashlib.sha256(b"parquet").hexdigest(),
                    }
                ],
            }
        ],
    }


def _run(tmp_path: Path, manifest: object, *, check_root: Path | None = None):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    command = [sys.executable, str(VERIFY_CLI), str(manifest_path)]
    if check_root is not None:
        command.extend(("--check-files", str(check_root)))
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.mark.parametrize(
    "schema",
    ["robot-vault-dataset-manifest/v1", "robot-vault-dataset-manifest/v999"],
)
def test_unknown_manifest_schema_fails_closed(tmp_path, schema):
    manifest = _manifest()
    manifest["schema"] = schema

    result = _run(tmp_path, manifest)

    assert result.returncode == 1
    assert "unsupported manifest schema" in _combined(result)
    assert "continuing anyway" not in _combined(result)
    assert "Traceback" not in _combined(result)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing_episodes", "missing episodes"),
        ("episodes_object", "$.episodes: expected an array"),
        ("episode_scalar", "$.episodes[0]: expected an object"),
        ("media_object", ".media: expected an array"),
        ("media_scalar", ".media[0]: expected an object"),
        ("media_missing_key", "missing key"),
        ("parquet_missing_sha", "missing sha256"),
        ("parquet_bad_digest", ".sha256: expected 64 hexadecimal"),
        ("parquet_null_digest", ".sha256: expected 64 hexadecimal"),
        ("parquet_null_size", ".file_size_bytes: expected an integer"),
    ],
)
def test_malformed_manifest_entries_fail_gracefully(tmp_path, case, expected):
    manifest = copy.deepcopy(_manifest())
    episode = manifest["episodes"][0]
    if case == "missing_episodes":
        manifest.pop("episodes")
    elif case == "episodes_object":
        manifest["episodes"] = {}
    elif case == "episode_scalar":
        manifest["episodes"][0] = "not-an-object"
    elif case == "media_object":
        episode["media"] = {}
    elif case == "media_scalar":
        episode["media"][0] = "not-an-object"
    elif case == "media_missing_key":
        episode["media"][0].pop("key")
    elif case == "parquet_missing_sha":
        episode["parquet"][0].pop("sha256")
    elif case == "parquet_bad_digest":
        episode["parquet"][0]["sha256"] = "not-a-digest"
    elif case == "parquet_null_digest":
        episode["parquet"][0]["sha256"] = None
    elif case == "parquet_null_size":
        episode["parquet"][0]["file_size_bytes"] = None
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(case)

    result = _run(tmp_path, manifest)

    assert result.returncode == 1
    assert expected in _combined(result)
    assert "malformed or unsupported manifest" in _combined(result)
    assert "Traceback" not in _combined(result)


def test_check_files_fails_when_media_and_parquet_are_missing(tmp_path):
    root = tmp_path / "lake"
    root.mkdir()
    manifest = _manifest()

    result = _run(tmp_path, manifest, check_root=root)

    assert result.returncode == 1
    output = _combined(result)
    assert "missing processed/media/chunk-000.mp4" in output
    assert "missing robotics-data/processed/parquet/part-000.parquet" in output
    assert "2 local file check(s) failed" in output


@pytest.mark.parametrize("escape", ["absolute", "parent"])
def test_artifact_paths_cannot_escape_the_verification_root(tmp_path, escape):
    content = b"media"
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(content)
    root = tmp_path / "lake"
    root.mkdir()
    manifest = _manifest()
    manifest["episodes"][0]["parquet"] = []
    manifest["episodes"][0]["media"][0]["key"] = (
        str(outside) if escape == "absolute" else "../outside.mp4"
    )

    result = _run(tmp_path, manifest, check_root=root)

    assert result.returncode == 1
    output = _combined(result)
    assert "artifact paths are not allowed" in output or "'..' path components" in output
    assert "Traceback" not in output


def test_symlink_target_outside_verification_root_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chunk-000.mp4").write_bytes(b"media")
    root = tmp_path / "lake"
    root.mkdir()
    (root / "processed").symlink_to(outside, target_is_directory=True)
    manifest = _manifest()
    manifest["episodes"][0]["parquet"] = []
    manifest["episodes"][0]["media"][0]["key"] = "processed/chunk-000.mp4"

    result = _run(tmp_path, manifest, check_root=root)

    assert result.returncode == 1
    output = _combined(result)
    assert "resolves outside verification root" in output
    assert "sha256 matches" not in output
    assert "Traceback" not in output
