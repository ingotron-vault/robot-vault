"""Fast refusal/static gates for the pinned frozen-agent producer."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "packaging/robot-agent"
FREEZE = PACKAGE / "freeze_agent.py"
# The builder recipe is a stage of the query image, which serves the agent.
QUERY_DOCKERFILE = "phases/05-query-service/Dockerfile"
SPEC = importlib.util.spec_from_file_location("agent_freeze", FREEZE)
freeze = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(freeze)


def _builder_fixture(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    package = source / "packaging/robot-agent"
    package.mkdir(parents=True)
    for name in (
        "build-agent-tarball.sh",
        "freeze_agent.py",
        "pack_agent.sh",
        "pyinstaller_entry.py",
        "requirements-build.lock.txt",
    ):
        shutil.copy2(PACKAGE / name, package / name)
    shutil.copy2(ROOT / "LICENSE", source / "LICENSE")
    (source / QUERY_DOCKERFILE).parent.mkdir(parents=True)
    shutil.copy2(ROOT / QUERY_DOCKERFILE, source / QUERY_DOCKERFILE)
    return source


@pytest.mark.parametrize(
    "missing",
    (
        QUERY_DOCKERFILE,
        "packaging/robot-agent/freeze_agent.py",
        "packaging/robot-agent/pack_agent.sh",
        "packaging/robot-agent/pyinstaller_entry.py",
        "packaging/robot-agent/requirements-build.lock.txt",
    ),
)
def test_builder_refuses_a_missing_immutable_authority_before_docker(
    tmp_path: Path, missing: str
):
    source = _builder_fixture(tmp_path)
    (source / missing).unlink()
    process = subprocess.run(
        [
            "bash",
            str(source / "packaging/robot-agent/build-agent-tarball.sh"),
            "--out",
            str(tmp_path / "agent.tar.gz"),
            "--version",
            "0.9.1",
            "--source-epoch",
            "0",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    assert process.returncode != 0
    assert "required agent build authority is missing" in process.stdout
    assert missing.rsplit("/", 1)[-1] in process.stdout


def test_builder_refuses_a_missing_root_license_before_docker(tmp_path: Path):
    """The agent ships the root LICENSE; it needs no file outside the core tree."""
    source = _builder_fixture(tmp_path)
    (source / "LICENSE").unlink()
    process = subprocess.run(
        [
            "bash",
            str(source / "packaging/robot-agent/build-agent-tarball.sh"),
            "--out",
            str(tmp_path / "agent.tar.gz"),
            "--version",
            "0.9.1",
            "--source-epoch",
            "0",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    assert process.returncode != 0
    assert "required agent build authority is missing" in process.stdout
    assert "/LICENSE" in process.stdout


def test_dependency_lock_is_an_exact_single_hash_linux_amd64_closure():
    text = (PACKAGE / "requirements-build.lock.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ \\\n]+)", text, re.M)
    hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})$", text, re.M)
    assert len(pins) == len(hashes) == 11
    assert len({name.lower() for name, _version in pins}) == 11
    assert {name.lower() for name, _version in pins} >= {
        "grpcio",
        "grpcio-tools",
        "prometheus-client",
        "protobuf",
        "psutil",
        "pyinstaller",
    }
    requirements = [
        line for line in text.splitlines() if line and not line.startswith(("#", " "))
    ]
    assert len(requirements) == 11
    assert all(
        line.endswith(" \\")
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[^ \\]+", line[:-2])
        for line in requirements
    )


def test_reproducibility_patch_refuses_an_unreviewed_writer_or_version():
    with pytest.raises(SystemExit, match="writer implementation changed"):
        freeze.verify_pyz_writer_authority(freeze.PYINSTALLER_VERSION, b"tampered")
    with pytest.raises(SystemExit, match="expected PyInstaller"):
        freeze.verify_pyz_writer_authority("999.0", b"tampered")


def test_builder_base_snapshot_and_elf_tool_are_exactly_pinned():
    text = (ROOT / QUERY_DOCKERFILE).read_text(encoding="utf-8")
    start = text.index(" AS robot-agent-builder\n")
    dockerfile = text[text.rindex("FROM ", 0, start):text.index("ENTRYPOINT ", start)]
    assert dockerfile.startswith(
        "FROM --platform=linux/amd64 " + freeze.BUILDER_IMAGE + " AS robot-agent-builder"
    )
    assert "snapshot.debian.org" in dockerfile
    assert "binutils=2.35.2-2" in dockerfile
    assert "binutils-x86-64-linux-gnu=2.35.2-2" in dockerfile
    assert "apt-get install -y --no-install-recommends" in dockerfile
    assert "--require-hashes --only-binary=:all: --no-deps" in dockerfile

