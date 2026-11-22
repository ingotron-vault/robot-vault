#!/usr/bin/env python3
"""Back up and restore a Robot Vault stack: the Postgres database and the object-store bucket.

Use it through Make (dev stack, or the release stack with RELEASE_ENV_FILE):

    make backup  BACKUP_DIR=/srv/backups/2026-11-22
    make restore BACKUP_DIR=/srv/backups/2026-11-22

The host side (this file, run with the host python3, stdlib only) drives Docker Compose:

    backup   stop every running service except postgres and objectstore, dump Postgres
             (pg_dump -Fc), copy the bucket with the toolkit image (`export`, below), write
             recovery-point.json last, publish BACKUP_DIR by an atomic rename, then start the
             stopped services again (also on failure).
    restore  preflight first (`verify`, below): any problem exits nonzero before anything in the
             stack changes. Then stop every service, start only postgres and objectstore, restore
             the objects (`import`, below), then the database. The other services stay stopped,
             also after success; start the stack again with `make stack-up` / `make release-up`.
    verify   the preflight alone. Reads only BACKUP_DIR.

The same file runs inside the release toolkit image (rclone 1.76.0 and boto3), mounted read-only,
in the network namespace of the running objectstore container:

    export   list and HEAD every object, `rclone copy` the bucket into objects/, `rclone check
             --download` (every byte compared), list again (the bucket must not have changed),
             then write objectstore-manifest.jsonl: key, size, SHA-256 of the bytes, content
             type and all user metadata, one JSON object per line.
    import   upload every manifest object with its content type and metadata (boto3), verify
             every object in the store against the manifest (bytes by SHA-256, size, content type,
             metadata), and only then delete the keys that are not in the manifest.

Recovery point layout (BACKUP_DIR):

    recovery-point.json         written last: bucket, size and SHA-256 of the dump and manifest
    postgres.dump               pg_dump -Fc
    objectstore-manifest.jsonl  one line per object, sorted by key
    objects/<key>               the object bytes; the path is always "objects/" + key

Secrets: the object-store root key is read from the running objectstore container over a pipe and
passed to the toolkit container by variable name (`-e NAME`), never as a command-line value.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import sys
import time

FORMAT = "robot-vault-backup"
VERSION = 1
RECORD = "recovery-point.json"
DUMP = "postgres.dump"
MANIFEST = "objectstore-manifest.jsonl"
OBJECTS = "objects"
DATASTORES = ("postgres", "objectstore")
ENTRY_KEYS = {"key", "size", "sha256", "content_type", "metadata"}
CHUNK = 1024 * 1024


class Refused(Exception):
    """A recovery point or an object store that must not be used."""


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# --- keys and the manifest (shared by both sides) ---------------------------


def key_problem(key: object) -> str | None:
    """Why an object key cannot be stored as objects/<key>, or None."""
    if not isinstance(key, str) or not key:
        return "the key is empty or not a string"
    if len(key.encode("utf-8", "surrogatepass")) > 1024:
        return "the key is longer than 1024 bytes"
    if "\x00" in key or any(0xD800 <= ord(c) <= 0xDFFF for c in key):
        return "the key contains NUL or an invalid character"
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "the key has an empty, '.' or '..' path segment (or starts or ends with '/')"
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        return "a path segment of the key is longer than 255 bytes"
    return None


def parse_manifest(data: bytes) -> list[dict]:
    """Parse and check the whole manifest. Raises Refused on the first problem."""
    entries: list[dict] = []
    seen: set[str] = set()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{MANIFEST} is not UTF-8")
    if text and not text.endswith("\n"):
        raise Refused(f"{MANIFEST} does not end with a line break (truncated?)")
    for number, line in enumerate(text.splitlines(), 1):
        where = f"{MANIFEST} line {number}"
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            raise Refused(f"{where} is not JSON")
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise Refused(f"{where} must have exactly the fields {sorted(ENTRY_KEYS)}")
        problem = key_problem(entry["key"])
        if problem:
            raise Refused(f"{where}: unsafe key {entry['key']!r}: {problem}")
        size, digest = entry["size"], entry["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise Refused(f"{where}: size must be a non-negative integer")
        if not isinstance(digest, str) or len(digest) != 64 or digest.strip("0123456789abcdef"):
            raise Refused(f"{where}: sha256 must be 64 lowercase hex characters")
        if not isinstance(entry["content_type"], str):
            raise Refused(f"{where}: content_type must be a string")
        metadata = entry["metadata"]
        if not isinstance(metadata, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) for k, v in metadata.items()
        ):
            raise Refused(f"{where}: metadata must map non-empty strings to strings")
        if entry["key"] in seen:
            raise Refused(f"{where}: duplicate key {entry['key']!r}")
        seen.add(entry["key"])
        entries.append(entry)
    for key in seen:
        parent = key.rsplit("/", 1)[0] if "/" in key else None
        while parent:
            if parent in seen:
                raise Refused(f"{MANIFEST}: key {parent!r} is also a folder of key {key!r}")
            parent = parent.rsplit("/", 1)[0] if "/" in parent else None
    return entries


# --- the preflight (host side; stdlib only) ----------------------------------


def load_record(root: pathlib.Path) -> dict:
    path = root / RECORD
    if path.is_symlink() or not path.is_file():
        raise Refused(f"{RECORD} is missing: the backup did not finish")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"{RECORD} cannot be read: {type(exc).__name__}")
    ok = (
        isinstance(record, dict)
        and record.get("format") == FORMAT
        and record.get("version") == VERSION
        and isinstance(record.get("bucket"), str)
        and all(
            isinstance(record.get(name), dict)
            and isinstance(record[name].get("size"), int)
            and isinstance(record[name].get("sha256"), str)
            for name in ("postgres_dump", "manifest")
        )
        and isinstance(record["manifest"].get("objects"), int)
    )
    if not ok:
        raise Refused(f"{RECORD} is not a version {VERSION} {FORMAT} record")
    return record


def check_file(root: pathlib.Path, name: str, size: int, digest: str) -> None:
    path = root / name
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise Refused(f"{name} is missing")
    if not stat.S_ISREG(mode):
        raise Refused(f"{name} is not a regular file")
    if path.stat().st_size != size:
        raise Refused(f"{name} has {path.stat().st_size} bytes, the recovery point recorded {size}")
    if sha256_file(path) != digest:
        raise Refused(f"{name} does not match its recorded SHA-256")


def verify_recovery_point(root: pathlib.Path) -> tuple[dict, list[dict]]:
    """Preflight: the whole recovery point is checked before anything changes."""
    if root.is_symlink() or not root.is_dir():
        raise Refused(f"{root} is not a directory")
    record = load_record(root)
    top = {entry.name for entry in os.scandir(root)}
    expected_top = {RECORD, DUMP, MANIFEST, OBJECTS}
    if top != expected_top:
        extra, missing = sorted(top - expected_top), sorted(expected_top - top)
        raise Refused(f"the backup folder must hold exactly {sorted(expected_top)} (extra {extra}, missing {missing})")
    check_file(root, DUMP, record["postgres_dump"]["size"], record["postgres_dump"]["sha256"])
    check_file(root, MANIFEST, record["manifest"]["size"], record["manifest"]["sha256"])
    entries = parse_manifest((root / MANIFEST).read_bytes())
    if len(entries) != record["manifest"]["objects"]:
        raise Refused(f"{MANIFEST} has {len(entries)} objects, the recovery point recorded {record['manifest']['objects']}")

    expected_files = {entry["key"]: entry for entry in entries}
    expected_dirs = {""}
    for key in expected_files:
        parts = key.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            expected_dirs.add("/".join(parts[:i]))
    objects = root / OBJECTS
    if objects.is_symlink() or not objects.is_dir():
        raise Refused(f"{OBJECTS}/ is not a directory")
    found: set[str] = set()
    stack = [objects]
    while stack:
        directory = stack.pop()
        for item in os.scandir(directory):
            relative = pathlib.Path(item.path).relative_to(objects).as_posix()
            mode = item.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                if relative not in expected_dirs:
                    raise Refused(f"extra folder in the payload: {OBJECTS}/{relative}")
                stack.append(pathlib.Path(item.path))
            elif stat.S_ISREG(mode):
                if relative not in expected_files:
                    raise Refused(f"extra payload not in the manifest: {OBJECTS}/{relative}")
                found.add(relative)
            else:
                raise Refused(f"payload is not a regular file: {OBJECTS}/{relative}")
    missing = sorted(set(expected_files) - found)
    if missing:
        raise Refused(f"payload missing for manifest key {missing[0]!r} ({len(missing)} missing)")
    for key, entry in sorted(expected_files.items()):
        path = objects / key
        if path.stat().st_size != entry["size"]:
            raise Refused(f"payload {OBJECTS}/{key} has the wrong size")
        if sha256_file(path) != entry["sha256"]:
            raise Refused(f"payload {OBJECTS}/{key} does not match its SHA-256 in the manifest")
    return record, entries


# --- toolkit side: rclone and boto3 against the store ------------------------


def store_settings() -> tuple[str, str, str, str]:
    endpoint = os.environ.get("S3_ENDPOINT", "http://127.0.0.1:9000")
    return endpoint, os.environ["S3_BUCKET"], os.environ["OBJECTSTORE_ROOT_USER"], os.environ["OBJECTSTORE_ROOT_PASSWORD"]


def s3_client():
    import boto3
    from botocore.config import Config

    endpoint, _, user, password = store_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=user,
        aws_secret_access_key=password,
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      retries={"max_attempts": 5, "mode": "standard"}),
    )


def rclone(*args: str) -> None:
    endpoint, _, user, password = store_settings()
    env = dict(
        os.environ,
        RCLONE_CONFIG="/dev/null",
        RCLONE_CONFIG_STORE_TYPE="s3",
        RCLONE_CONFIG_STORE_PROVIDER="Other",
        RCLONE_CONFIG_STORE_ENDPOINT=endpoint,
        RCLONE_CONFIG_STORE_REGION=os.environ.get("S3_REGION", "us-east-1"),
        RCLONE_CONFIG_STORE_FORCE_PATH_STYLE="true",
        RCLONE_CONFIG_STORE_ACCESS_KEY_ID=user,
        RCLONE_CONFIG_STORE_SECRET_ACCESS_KEY=password,
    )
    result = subprocess.run(["rclone", *args], env=env)
    if result.returncode != 0:
        raise Refused(f"rclone {args[0]} failed with exit {result.returncode}")


def list_bucket(s3, bucket: str) -> dict[str, tuple[int, str]]:
    listing: dict[str, tuple[int, str]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            listing[item["Key"]] = (item["Size"], item["ETag"])
    return listing


def container_export(root: pathlib.Path) -> None:
    _, bucket, _, _ = store_settings()
    s3 = s3_client()
    listing = list_bucket(s3, bucket)
    for key in sorted(listing):
        problem = key_problem(key)
        if problem:
            raise Refused(f"object {key!r} cannot be backed up: {problem}")
    heads = {}
    for key in sorted(listing):
        head = s3.head_object(Bucket=bucket, Key=key)
        heads[key] = (head["ContentType"], dict(head.get("Metadata", {})))
    objects = root / OBJECTS
    objects.mkdir(exist_ok=True)
    rclone("copy", f"store:{bucket}", str(objects))
    rclone("check", "--download", f"store:{bucket}", str(objects))
    if list_bucket(s3, bucket) != listing:
        raise Refused("the bucket changed during the backup; stop every writer and try again")
    lines = []
    for key in sorted(listing):
        path = objects / key
        if path.is_symlink() or not path.is_file() or path.stat().st_size != listing[key][0]:
            raise Refused(f"object {key!r} was not copied completely")
        content_type, metadata = heads[key]
        lines.append(json.dumps({"key": key, "size": listing[key][0], "sha256": sha256_file(path),
                                 "content_type": content_type, "metadata": metadata},
                                sort_keys=True, separators=(",", ":")))
    (root / MANIFEST).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    entries = parse_manifest((root / MANIFEST).read_bytes())
    found = {p.relative_to(objects).as_posix() for p in objects.rglob("*") if not p.is_dir()}
    if found != {entry["key"] for entry in entries}:
        raise Refused("the copied files do not match the bucket listing")
    print(json.dumps({"objects": len(entries), "bytes": sum(e["size"] for e in entries)}))


def stored_sha256(s3, bucket: str, key: str) -> str:
    digest = hashlib.sha256()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in body.iter_chunks(CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def container_import(root: pathlib.Path) -> None:
    from botocore.exceptions import ClientError

    _, bucket, _, _ = store_settings()
    s3 = s3_client()
    entries = parse_manifest((root / MANIFEST).read_bytes())
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
            raise
        s3.create_bucket(Bucket=bucket)
    # 1. upload every expected object, 2. verify all of them, 3. only then delete extra keys.
    for entry in entries:
        s3.upload_file(str(root / OBJECTS / entry["key"]), bucket, entry["key"],
                       ExtraArgs={"ContentType": entry["content_type"], "Metadata": entry["metadata"]})
    for entry in entries:
        key = entry["key"]
        head = s3.head_object(Bucket=bucket, Key=key)
        actual = (head["ContentLength"], head["ContentType"], dict(head.get("Metadata", {})))
        if actual != (entry["size"], entry["content_type"], entry["metadata"]):
            raise Refused(f"object {key!r} in the store does not match the manifest after upload")
        if stored_sha256(s3, bucket, key) != entry["sha256"]:
            raise Refused(f"object {key!r} in the store does not match its SHA-256 after upload")
    expected = {entry["key"] for entry in entries}
    extra = sorted(set(list_bucket(s3, bucket)) - expected)
    for start in range(0, len(extra), 1000):
        batch = [{"Key": key} for key in extra[start:start + 1000]]
        result = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": False})
        if result.get("Errors"):
            raise Refused(f"could not delete {len(result['Errors'])} keys that are not in the backup")
    if set(list_bucket(s3, bucket)) != expected:
        raise Refused("the bucket does not hold exactly the manifest keys after the restore")
    print(json.dumps({"objects": len(entries), "deleted": len(extra)}))


# --- host side: Docker Compose -----------------------------------------------


class Stack:
    def __init__(self, compose: str, image: str) -> None:
        self.compose = shlex.split(compose)
        self.image = image

    def run(self, *args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
        result = subprocess.run([*self.compose, *args], **kwargs)
        if check and result.returncode != 0:
            raise Refused(f"`docker compose {' '.join(args[:3])}` failed with exit {result.returncode}")
        return result

    def output(self, *args: str) -> str:
        return self.run(*args, stdout=subprocess.PIPE, text=True).stdout

    def running(self) -> list[str]:
        return self.output("ps", "--status", "running", "--services").split()

    def start_datastores(self) -> None:
        self.run("up", "-d", "--no-deps", "--wait", *DATASTORES)

    def bucket(self) -> str:
        model = json.loads(self.output("config", "--format", "json"))
        env = model["services"].get("objectstore-access-ready", {}).get("environment") or {}
        return env.get("S3_BUCKET") or "robotics-data"

    def root_key(self) -> tuple[str, str]:
        # Read inside the store container and returned over a pipe: no secret in any argv.
        out = self.output("exec", "-T", "objectstore", "sh", "-c",
                          'printf "%s\\n" "$RUSTFS_ACCESS_KEY"; cat "$RUSTFS_SECRET_KEY_FILE"')
        user, _, password = out.partition("\n")
        password = password.rstrip("\n")
        if not user or not password:
            raise Refused("could not read the object-store root key from the objectstore container")
        return user, password

    def toolkit(self, command: str, folder: pathlib.Path, bucket: str, read_only: bool) -> None:
        container = self.output("ps", "-q", "objectstore").strip()
        if not container:
            raise Refused("the objectstore container is not running")
        user, password = self.root_key()
        env = dict(os.environ, OBJECTSTORE_ROOT_USER=user, OBJECTSTORE_ROOT_PASSWORD=password, S3_BUCKET=bucket)
        tool = pathlib.Path(__file__).resolve()
        args = [
            "docker", "run", "--rm", "--network", f"container:{container}",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{folder.resolve()}:/backup{':ro' if read_only else ''}",
            "-v", f"{tool}:/opt/robot-vault-backup/robot_vault_backup.py:ro",
            "-e", "OBJECTSTORE_ROOT_USER", "-e", "OBJECTSTORE_ROOT_PASSWORD", "-e", "S3_BUCKET",
            "-e", "HOME=/tmp", "--entrypoint", "python3", self.image,
            "/opt/robot-vault-backup/robot_vault_backup.py", command, "/backup",
        ]
        if subprocess.run(args, env=env).returncode != 0:
            raise Refused(f"the object-store {command} failed")


METRICS_FILE = "robot-vault-backup.prom"
METRIC_NAMES = ("last_success_timestamp_seconds", "last_failure_timestamp_seconds", "last_run_success")


def write_metrics(folder: str | None, ok: bool) -> None:
    """The ops-metrics backup source: three `robot_vault_backup_<name> <integer>` lines,
    Unix seconds (0 = never), written to a temp file in the same folder and renamed. Each run rewrites
    all three and carries the other timestamp from the previous file."""
    if not folder:
        return
    path = pathlib.Path(folder) / METRICS_FILE
    values = {name: 0 for name in METRIC_NAMES}
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            name, value = line.split(" ")
            if name.startswith("robot_vault_backup_") and name[19:] in values:
                values[name[19:]] = int(value)
    except (OSError, ValueError):
        pass
    now = int(time.time())
    if ok:
        values["last_success_timestamp_seconds"] = now
    else:
        values["last_failure_timestamp_seconds"] = now
    values["last_run_success"] = int(ok)
    tmp = path.with_name(f".{METRICS_FILE}.tmp")
    try:
        tmp.write_text("".join(f"robot_vault_backup_{n} {values[n]}\n" for n in METRIC_NAMES), encoding="ascii")
        tmp.chmod(0o644)
        os.replace(tmp, path)
    except OSError as exc:  # the backup result stands; the stale file makes the freshness alert fire
        print(f"warning: could not write {path}: {exc}", file=sys.stderr)


def host_backup(stack: Stack, final: pathlib.Path, metrics_dir: str | None = None) -> None:
    try:
        backup_to(stack, final)
    except BaseException:
        write_metrics(metrics_dir, ok=False)
        raise
    write_metrics(metrics_dir, ok=True)


def backup_to(stack: Stack, final: pathlib.Path) -> None:
    if final.exists() or final.is_symlink():
        raise Refused(f"{final} already exists; choose a new BACKUP_DIR")
    partial = final.with_name(final.name + ".partial")
    if partial.exists():
        raise Refused(f"{partial} is left from a failed backup; remove it first")
    stack.start_datastores()
    stopped = [s for s in stack.running() if s not in DATASTORES]
    if stopped:
        say(f"stopping writers for a consistent copy: {' '.join(stopped)}")
        stack.run("stop", *stopped)
    try:
        partial.mkdir(parents=True, mode=0o700)
        bucket = stack.bucket()
        say("dumping Postgres (pg_dump -Fc)")
        with (partial / DUMP).open("wb") as out:
            stack.run("exec", "-T", "postgres", "sh", "-c", 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"', stdout=out)
        say(f"copying bucket {bucket} (rclone copy, rclone check --download, manifest)")
        stack.toolkit("export", partial, bucket, read_only=False)
        entries = parse_manifest((partial / MANIFEST).read_bytes())
        record = {
            "format": FORMAT, "version": VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bucket": bucket,
            "postgres_dump": {"size": (partial / DUMP).stat().st_size, "sha256": sha256_file(partial / DUMP)},
            "manifest": {"size": (partial / MANIFEST).stat().st_size, "sha256": sha256_file(partial / MANIFEST),
                         "objects": len(entries)},
        }
        (partial / RECORD).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        verify_recovery_point(partial)
        subprocess.run(["sync"], check=False)
        partial.rename(final)
        say(f"backup complete: {final} ({len(entries)} objects)")
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    finally:
        if stopped:
            say(f"starting the stopped services again: {' '.join(stopped)}")
            stack.run("start", *stopped, check=False)


def host_restore(stack: Stack, folder: pathlib.Path) -> None:
    say(f"preflight: checking every file in {folder}")
    record, entries = verify_recovery_point(folder)
    say(f"preflight passed: {len(entries)} objects, bucket {record['bucket']}")
    bucket = stack.bucket()
    if bucket != record["bucket"]:
        raise Refused(f"the backup is of bucket {record['bucket']!r}, but this stack uses {bucket!r}")
    say("stopping every service; they stay stopped until you start the stack again")
    stack.run("stop")
    stack.start_datastores()
    say("restoring objects: upload, verify every object, then delete keys not in the backup")
    stack.toolkit("import", folder, bucket, read_only=True)
    say("restoring Postgres (pg_restore --clean --if-exists --single-transaction)")
    with (folder / DUMP).open("rb") as dump:
        stack.run("exec", "-T", "postgres", "sh", "-c",
                  'pg_restore --clean --if-exists --single-transaction --exit-on-error '
                  '-U "$POSTGRES_USER" -d "$POSTGRES_DB"', stdin=dump)
    say("restore complete. Start the stack again: `make stack-up` (dev) or `make release-up` (release)")


def main(argv: list[str]) -> int:
    usage = (
        "usage: robot_vault_backup.py backup|restore --dir DIR --compose 'docker compose ...' --image IMAGE"
        " [--metrics-dir DIR]\n"
        "       robot_vault_backup.py verify DIR\n"
        "       robot_vault_backup.py export|import DIR   (inside the toolkit image)"
    )
    if len(argv) == 2 and argv[0] in ("verify", "export", "import"):
        command, folder = argv[0], pathlib.Path(argv[1])
        try:
            if command == "verify":
                _, entries = verify_recovery_point(folder)
                print(f"recovery point OK: {len(entries)} objects")
            elif command == "export":
                container_export(folder)
            else:
                container_import(folder)
        except Refused as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    metrics_dir = None
    if len(argv) == 9 and argv[0] == "backup" and argv[7] == "--metrics-dir":
        metrics_dir, argv = argv[8], argv[:7]
    if len(argv) == 7 and argv[0] in ("backup", "restore") and argv[1::2] == ["--dir", "--compose", "--image"]:
        command = argv[0]
        stack = Stack(argv[4], argv[6])
        try:
            if command == "backup":
                host_backup(stack, pathlib.Path(argv[2]), metrics_dir)
            else:
                host_restore(stack, pathlib.Path(argv[2]))
        except Refused as exc:
            note = "" if command == "backup" else " Services stopped by the restore stay stopped."
            print(f"error: {exc}.{note}", file=sys.stderr)
            return 1
        return 0
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
