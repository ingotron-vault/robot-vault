#!/usr/bin/env python3
"""Build the self-contained Linux/amd64 PyInstaller onedir staging tree.

This program runs only inside the ``robot-agent-builder`` stage of
``phases/05-query-service/Dockerfile``.  The source is read from a stable path
and the destination is a writable directory.  Keeping the freeze separate from
the outer tar step (``pack_agent.sh``) lets the latter apply canonical
ownership, modes, ordering, mtimes, and gzip headers after PyInstaller has
finished.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata
import inspect
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zlib


ROOT_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
STUBS = (
    "upload_pb2.py",
    "upload_pb2_grpc.py",
    "enrollment_pb2.py",
    "enrollment_pb2_grpc.py",
)
BUILDER_IMAGE = (
    "python:3.10.18-slim-bullseye@"
    "sha256:b3061b93c8df9809c3783a4f17bbf2520425ec6b40bd3e5e7538870e21ba7209"
)
PYINSTALLER_VERSION = "6.22.2"
PYZ_WRITER_SOURCE_SHA256 = "aecd7595da1a115b703354597b0212a6d95d25dfc7d89f92bb2ef5314c9452df"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"agent freeze: required regular file is missing: {path}")
    return path


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"agent freeze: required directory is missing: {source}")
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )


def verify_pyz_writer_authority(actual_version: str, writer_source: bytes) -> None:
    """Refuse to patch anything except the reviewed pinned writer."""
    if actual_version != PYINSTALLER_VERSION:
        raise SystemExit(
            f"agent freeze: expected PyInstaller {PYINSTALLER_VERSION}, got {actual_version}"
        )
    actual_source_hash = hashlib.sha256(writer_source).hexdigest()
    if actual_source_hash != PYZ_WRITER_SOURCE_SHA256:
        raise SystemExit(
            "agent freeze: pinned PyInstaller PYZ writer implementation changed "
            f"({actual_source_hash})"
        )


def install_reproducible_pyz_writer() -> None:
    """Install the pinned, format-aware PYZ marshal normalization.

    CPython 3.10 can encode an otherwise identical interned string with or
    without ``FLAG_REF`` depending on identity sharing in the freshly compiled
    code graph.  PyInstaller's normal writer marshals that graph directly,
    which made the frozen ``subprocess`` module alternate by one byte.  A
    marshal round trip first gives every module a canonical identity graph;
    the second marshal is the byte stream placed in the PYZ.

    This patch is intentionally bound to the exact PyInstaller implementation
    installed from the hashed lock.  A dependency update must review and
    update the implementation hash instead of silently applying this writer to
    a new archive format.
    """
    import PyInstaller.archive.writers as writers

    writer_source = inspect.getsource(writers.ZlibArchiveWriter._write_entry).encode()
    verify_pyz_writer_authority(
        importlib.metadata.version("pyinstaller"), writer_source
    )

    def write_entry(cls, fp, entry, code_dict):
        name, src_path, typecode = entry
        if typecode not in {"PYMODULE", "PYMODULE-1", "PYMODULE-2"}:
            raise AssertionError(f"unexpected PYZ entry type: {typecode}")
        if src_path in {"-", None}:
            return (name, (writers.PYZ_ITEM_NSPKG, fp.tell(), 0))

        code_object = code_dict[name]
        src_basename, _ = os.path.splitext(os.path.basename(src_path))
        if src_basename == "__init__":
            typecode = writers.PYZ_ITEM_PKG
            code_filename = os.path.join(*name.split("."), "__init__.py")
        else:
            typecode = writers.PYZ_ITEM_MODULE
            code_filename = os.path.join(*name.split(".")) + ".py"

        code_object = writers.replace_filename_in_code_object(code_object, code_filename)
        code_object = marshal.loads(marshal.dumps(code_object))
        data = marshal.dumps(code_object)
        compressed = zlib.compress(data, cls._COMPRESSION_LEVEL)
        toc_entry = (name, (typecode, fp.tell(), len(compressed)))
        fp.write(compressed)
        return toc_entry

    writers.ZlibArchiveWriter._write_entry = classmethod(write_entry)


def locked_components(lock: Path) -> list[dict[str, str]]:
    text = lock.read_text(encoding="utf-8")
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ \\]+)", line)
        if match:
            current = {"name": match.group(1), "version": match.group(2)}
            rows.append(current)
            continue
        hash_match = re.search(r"--hash=sha256:([0-9a-f]{64})", line)
        if hash_match and current is not None:
            if "sha256" in current:
                raise SystemExit(f"agent freeze: multiple wheel hashes for {current['name']}")
            current["sha256"] = hash_match.group(1)
    if not rows or any("sha256" not in row for row in rows):
        raise SystemExit("agent freeze: dependency lock is empty or has an unhashed pin")
    return rows


def copy_dependency_licenses(output: Path, components: list[dict[str, str]]) -> None:
    destination = output / "THIRD_PARTY_LICENSES"
    destination.mkdir()
    python_license = require_file(Path(sys.prefix) / "lib/python3.10/LICENSE.txt")
    shutil.copy2(python_license, destination / "Python-3.10.18-LICENSE.txt")

    missing: list[str] = []
    for row in components:
        distribution = importlib.metadata.distribution(row["name"])
        copied = 0
        for relative in distribution.files or ():
            basename = Path(str(relative)).name.lower()
            if not basename.startswith(("license", "copying", "notice")):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            target_dir = destination / f"{row['name']}-{row['version']}"
            target_dir.mkdir(exist_ok=True)
            target = target_dir / Path(str(relative)).name
            if target.exists() and target.read_bytes() != source.read_bytes():
                target = target_dir / str(relative).replace("/", "_")
            shutil.copy2(source, target)
            copied += 1
        # grpcio-tools is produced by the same gRPC project/distribution terms
        # and its wheel carries no duplicate text; grpcio's exact LICENSE is
        # included above. Every other locked distribution currently carries a
        # notice and a publisher dropping it must stop the release build.
        if copied == 0 and row["name"] != "grpcio-tools":
            missing.append(row["name"])
    if missing:
        raise SystemExit("agent freeze: locked distributions have no licence files: " + ", ".join(missing))


def write_dependency_bom(
    output: Path,
    version: str,
    source_epoch: int,
    lock_sha256: str,
    components: list[dict[str, str]],
) -> None:
    runtime = {"grpcio", "prometheus-client", "protobuf", "psutil", "typing-extensions"}
    both = {"pyinstaller"}  # its bootloader is part of the frozen executable
    bom_components = []
    for row in components:
        name = row["name"]
        scope = "runtime" if name in runtime else ("build-and-runtime" if name in both else "build")
        bom_components.append(
            {
                "bom-ref": f"pkg:pypi/{name.lower().replace('_', '-')}@{row['version']}",
                "hashes": [{"alg": "SHA-256", "content": row["sha256"]}],
                "name": name,
                "properties": [
                    {"name": "ingotron:agent-scope", "value": scope},
                    {"name": "ingotron:hash-subject", "value": "locked Linux/amd64 wheel"},
                ],
                "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{row['version']}",
                "type": "library",
                "version": row["version"],
            }
        )
    moment = datetime.datetime.fromtimestamp(source_epoch, datetime.timezone.utc)
    bom = {
        "bomFormat": "CycloneDX",
        "components": bom_components,
        "metadata": {
            "component": {
                "bom-ref": f"pkg:generic/ingotron-agent@{version}?arch=amd64&os=linux",
                "name": "ingotron-agent",
                "properties": [
                    {"name": "ingotron:builder-image", "value": BUILDER_IMAGE},
                    {
                        "name": "ingotron:boundary",
                        "value": (
                            "locked build/runtime dependency BOM; final binary/OS-package scan remains "
                            "a release-time check"
                        ),
                    },
                ],
                "type": "application",
                "version": version,
            },
            "timestamp": moment.isoformat().replace("+00:00", "Z"),
            "tools": {
                "components": [
                    {"name": "freeze_agent.py", "type": "application", "version": "1"}
                ]
            },
        },
        "serialNumber": "urn:uuid:" + str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"ingotron-agent:{version}:{lock_sha256}")
        ),
        "specVersion": "1.5",
        "version": 1,
    }
    (output / "AGENT_DEPENDENCY_SBOM.cdx.json").write_text(
        json.dumps(bom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-epoch", type=int, required=True)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output_root.resolve()
    if not source.is_dir():
        raise SystemExit(f"agent freeze: source is not a directory: {source}")
    if not ROOT_RE.fullmatch(args.version):
        raise SystemExit(f"agent freeze: version is not strict semver: {args.version!r}")
    if args.source_epoch < 0:
        raise SystemExit("agent freeze: source epoch must be non-negative")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"agent freeze: output root is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    # The agent is core code: its licence is the repository's root LICENSE.
    # Copy bytes; never synthesize or edit legal text here.
    license_file = require_file(source / "LICENSE")

    work = output.parent / ".freeze-work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    stubs = work / "stubs"
    stubs.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "-I",
            str(source / "proto"),
            f"--python_out={stubs}",
            f"--grpc_python_out={stubs}",
            str(source / "proto/upload.proto"),
            str(source / "proto/enrollment.proto"),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    for name in STUBS:
        require_file(stubs / name)

    dist = work / "dist"
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(work / "home"),
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(args.source_epoch),
        }
    )
    Path(env["HOME"]).mkdir()
    paths = (
        source / "packaging/robot-agent/agent/lib",
        source / "phases/common-config/src",
        source / "phases/01-robot-storage/src",
        source / "phases/02-robot-upload-agent/src",
        stubs,
    )
    command = [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noupx",
        "--name",
        "ingotron-agent",
        "--contents-directory",
        "_internal",
        "--distpath",
        str(dist),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work / "spec"),
    ]
    for path in paths:
        command.extend(("--paths", str(path)))
    for module in (
        "grpc",
        "enrollment_pb2",
        "enrollment_pb2_grpc",
        "upload_pb2",
        "upload_pb2_grpc",
        "robot_storage_manager.cli",
        "robot_upload_agent.cli",
        "prometheus_client",
        "psutil",
    ):
        command.extend(("--hidden-import", module))
    # grpc._runtime_protos imports grpc_tools for the optional run-time .proto
    # compiler API. The agent uses only the generated enrollment/upload stubs,
    # so shipping the 7.6 MiB compiler extension would turn a build-only pin
    # into an undeclared runtime surface for no product path.
    command.extend(("--exclude-module", "grpc_tools"))
    command.append(str(source / "packaging/robot-agent/pyinstaller_entry.py"))
    # The container set PYTHONHASHSEED before this interpreter started; the
    # assignment below preserves that value for build helpers but cannot and
    # does not attempt to change the running interpreter's seed retroactively.
    # PyInstaller runs in-process so the reviewed writer patch applies while
    # it creates the PYZ. This is producer-side normalization, never a binary
    # edit after the executable has been written.
    install_reproducible_pyz_writer()
    old_environment = {
        key: os.environ.get(key)
        for key in ("HOME", "PYTHONHASHSEED", "SOURCE_DATE_EPOCH")
    }
    os.environ.update(
        {
            "HOME": env["HOME"],
            "PYTHONHASHSEED": env["PYTHONHASHSEED"],
            "SOURCE_DATE_EPOCH": env["SOURCE_DATE_EPOCH"],
        }
    )
    try:
        from PyInstaller.__main__ import run as run_pyinstaller

        run_pyinstaller(command)
    finally:
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    frozen = dist / "ingotron-agent"
    require_file(frozen / "ingotron-agent")
    copy_tree(frozen, output / "bin")

    # The operator executable is the PyInstaller bootloader.  The other two
    # entries remain tiny POSIX launchers but can only exec that same frozen
    # executable; neither has a host-Python/source-tree escape hatch.
    for name in ("ingotron-agent-run", "ingotron-agent-retention"):
        shutil.copy2(
            require_file(source / f"packaging/robot-agent/agent/bin/{name}"),
            output / "bin" / name,
        )
    copy_tree(source / "packaging/robot-agent/agent/systemd", output / "systemd")
    copy_tree(source / "packaging/robot-agent/agent/config", output / "config")

    (output / "VERSION").write_text(args.version + "\n", encoding="utf-8")
    shutil.copy2(license_file, output / "LICENSE")
    shutil.copy2(
        require_file(source / "packaging/robot-agent/requirements-build.lock.txt"),
        output / "AGENT_DEPENDENCIES.lock.txt",
    )
    components = locked_components(output / "AGENT_DEPENDENCIES.lock.txt")
    copy_dependency_licenses(output, components)
    dependency_lock_sha256 = sha256(output / "AGENT_DEPENDENCIES.lock.txt")
    write_dependency_bom(
        output, args.version, args.source_epoch, dependency_lock_sha256, components
    )
    # The agent is core (AGPL-3.0): it carries the core notices.
    shutil.copy2(
        require_file(source / "THIRD_PARTY_NOTICES.md"),
        output / "THIRD_PARTY_NOTICES.md",
    )
    # Optional hook: when an inventory generator exists at this path, the
    # bundle also gets THIRD_PARTY_INVENTORY.json. Without it, the bundle
    # carries the agent's own lock, BOM and licence files.
    inventory_tool = source / "deploy/release-images/generate_third_party_inventory.py"
    if inventory_tool.is_file():
        subprocess.run(
            [
                sys.executable,
                str(inventory_tool),
                "--source",
                str(source),
                "--output",
                str(output / "THIRD_PARTY_INVENTORY.json"),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
        )

    build_info = {
        "builder_image": BUILDER_IMAGE,
        "dependency_lock": {
            "path": "AGENT_DEPENDENCIES.lock.txt",
            "sha256": dependency_lock_sha256,
        },
        "glibc_min": "2.31",
        "platform": "linux/amd64",
        "python": "3.10.18",
        "source_date_epoch": args.source_epoch,
        "version": args.version,
    }
    (output / "BUILD_INFO.json").write_text(
        json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    leaked = [path for path in output.rglob("*") if path.is_symlink()]
    if leaked:
        raise SystemExit(f"agent freeze: output contains links: {leaked[:3]}")
    source_files = [path for path in output.rglob("*.py") if path.is_file()]
    if source_files:
        raise SystemExit(f"agent freeze: Python source leaked into bundle: {source_files[:3]}")
    build_only = [
        path
        for path in output.rglob("*")
        if path.is_file()
        and (
            "grpc_tools" in path.parts
            or path.name
            in {"ar", "as", "ld", "nm", "objdump", "ranlib", "readelf", "strip"}
        )
    ]
    if build_only:
        raise SystemExit(f"agent freeze: build-only tools leaked into bundle: {build_only[:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
