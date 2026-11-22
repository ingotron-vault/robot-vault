"""Fault tests against disposable PostgreSQL, the object store (RustFS), and the real ingest binary.

Run after building ingest and regenerating Python proto stubs::

    INGEST_RESUME_BINARY=/absolute/path/ingest-server \
      PYTHONPATH=... python3 -m pytest tests/e2e/test_upload_resume_e2e.py -v -s

The fixture creates its own Docker network and containers, publishes ports on
loopback only, and removes only those resources. Existing stacks and volumes
are never used. A new client process resumes after an ingest process restart.

The object store is the pinned RustFS image with the settings of
development/objectstore/CONTRACT.md. The five service keys come from the product bootstrap
program (development/objectstore/bootstrap.py), so ingest and GC run with the release
permissions. The root key only starts the store, runs the bootstrap and inspects results.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
BINARY = os.environ.get("INGEST_RESUME_BINARY")
pytestmark = pytest.mark.skipif(not BINARY, reason="set INGEST_RESUME_BINARY to run isolated Docker fault tests")

# Match the ingest Dockerfile's Rust builder runtime. Image overrides permit
# testing an already-built image on offline hosts without changing fixtures.
RUNTIME_IMAGE = os.environ.get("INGEST_RESUME_RUNTIME_IMAGE", "rust@sha256:f4f82b80e5f2945fed4ba17af177c6d6be85d98cde38ff318fc7666ce4505617")
POSTGRES_IMAGE = os.environ.get("INGEST_RESUME_POSTGRES_IMAGE", "postgres@sha256:5660c2cbfea50c7a9127d17dc4e48543eedd3d7a41a595a2dfa572471e37e64c")
# The store is the pinned RustFS base of the first-party objectstore image
# (deploy/release-images/objectstore.Dockerfile, development/objectstore/CONTRACT.md section 1).
OBJECTSTORE_IMAGE = os.environ.get("INGEST_RESUME_OBJECTSTORE_IMAGE") or re.search(
    r"^FROM\s+(rustfs/rustfs:\S+)\s*$",
    (ROOT / "deploy/release-images/objectstore.Dockerfile").read_text(), re.M).group(1)
ROOT_USER, ROOT_PASSWORD = "resume-test-root", "resume-test-root-password-01"
ROLES = ("ingest", "processor", "exporter", "query", "gc")


def run(*args: str, timeout: float = 60) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def wait_until(check, description: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except Exception as exc:
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}: {last}")


class IsolatedIngest:
    def __init__(self, binary: Path):
        self.binary = binary
        self.prefix = f"rv-resume-{uuid.uuid4().hex[:10]}"
        self.network = self.prefix
        self.containers: list[str] = []
        self.bucket = "resume-fault-tests"
        self.secret_dir = Path(tempfile.mkdtemp(prefix="rv-resume-secret-"))
        self.token = "isolated-resume-test-token"
        self.ingest_database_url = "postgres://robot_vault_ingest:resume-ingest-db-password@postgres:5432/robotvault"

    def container(self, label, image, *args, options=()):
        name = f"{self.prefix}-{label}"
        self.containers.append(name)
        run("docker", "run", "-d", "--name", name, "--network", self.network,
            "--network-alias", label, *options, image, *args)
        return name

    def port(self, name, internal):
        return int(run("docker", "port", name, f"{internal}/tcp").rsplit(":", 1)[1])

    def start(self):
        import boto3
        import psycopg
        from botocore.config import Config

        run("docker", "network", "create", self.network)
        self.pg = self.container("postgres", POSTGRES_IMAGE, options=(
            "--tmpfs", "/var/lib/postgresql/data", "-e", "POSTGRES_USER=robotvault",
            "-e", "POSTGRES_PASSWORD=robotvault", "-e", "POSTGRES_DB=robotvault",
            "-p", "127.0.0.1::5432"))
        self.database_url = f"postgresql://robotvault:robotvault@127.0.0.1:{self.port(self.pg, 5432)}/robotvault"
        def postgres_ready():
            with psycopg.connect(self.database_url) as conn:
                return conn.execute("SELECT 1").fetchone()[0]
        wait_until(postgres_ready, "isolated PostgreSQL")
        subprocess.run([sys.executable, str(ROOT / "db/migrate.py")], check=True,
                       env={**os.environ, "DATABASE_URL": self.database_url}, capture_output=True, text=True)
        with psycopg.connect(self.database_url) as conn:
            conn.execute("INSERT INTO robots (id, name) VALUES ('resume-test-robot', 'Resume fault test')")
        role_env = {**os.environ, "DATABASE_URL": self.database_url}
        for role in ("ingest", "processor", "exporter", "query", "gc"):
            role_env[f"{role.upper()}_DATABASE_URL"] = self.database_url.replace(
                "robotvault:robotvault@", f"robot_vault_{role}:resume-{role}-db-password@")
        self.gc_database_url = role_env["GC_DATABASE_URL"]
        subprocess.run([sys.executable, str(ROOT / "development/release/bootstrap_postgres_roles.py")],
                       check=True, env=role_env, capture_output=True, text=True)
        password_file = self.secret_dir / "objectstore_root_password"
        password_file.write_text(ROOT_PASSWORD)
        password_file.chmod(0o644)
        # development/objectstore/CONTRACT.md section 2. `--user 0` only because the tmpfs data folder is owned by root.
        settings = {"RUSTFS_ACCESS_KEY": ROOT_USER, "RUSTFS_SECRET_KEY_FILE": "/run/secrets/objectstore_root_password",
                    "RUSTFS_VOLUMES": "/data", "RUSTFS_ADDRESS": "0.0.0.0:9000", "RUSTFS_CONSOLE_ENABLE": "false",
                    "RUSTFS_DURABILITY_MODE": "strict", "RUSTFS_DRIVE_SYNC_ENABLE": "true",
                    "RUSTFS_NEW_BUCKET_DURABILITY_MODE": "strict", "RUSTFS_PUT_RENAME_EARLY_ACK_ENABLE": "false",
                    "RUSTFS_API_STALE_UPLOADS_EXPIRY": "360h", "RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL": "6h"}
        self.objectstore = self.container("objectstore", OBJECTSTORE_IMAGE, options=(
            "--user", "0", "--tmpfs", "/data", "-v", f"{password_file}:/run/secrets/objectstore_root_password:ro",
            *[a for k, v in settings.items() for a in ("-e", f"{k}={v}")], "-p", "127.0.0.1::9000"))
        self.endpoint = f"http://127.0.0.1:{self.port(self.objectstore, 9000)}"
        wait_until(lambda: urllib.request.urlopen(f"{self.endpoint}/health/ready", timeout=2).status == 200,
                   "isolated object store", timeout=60)
        keys = {f"{r.upper()}_S3_ACCESS_KEY": f"robot-vault-{r}" for r in ROLES}
        keys.update({f"{r.upper()}_S3_SECRET_KEY": f"resume-{r}-s3-password" for r in ROLES})
        provision = subprocess.run(
            [sys.executable, str(ROOT / "development/objectstore/bootstrap.py")], capture_output=True, text=True, timeout=180,
            env={"PATH": os.environ.get("PATH", ""), "S3_ENDPOINT": self.endpoint, "S3_BUCKET": self.bucket,
                 "S3_REGION": "us-east-1", "OBJECTSTORE_ROOT_USER": ROOT_USER, "OBJECTSTORE_ROOT_PASSWORD": ROOT_PASSWORD,
                 "OBJECTSTORE_POLICY_DIR": str(ROOT / "development/release/objectstore-policies"), **keys})
        assert provision.returncode == 0, provision.stdout + provision.stderr

        def client(access, secret):
            return boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=access, aws_secret_access_key=secret,
                                region_name="us-east-1", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
        # The root client only inspects results; the GC client has the release `robot-vault-gc` permissions.
        self.s3 = client(ROOT_USER, ROOT_PASSWORD)
        self.gc_s3 = client("robot-vault-gc", "resume-gc-s3-password")
        self.start_ingest()
        return self

    def start_ingest(self):
        import grpc
        self.ingest = self.container("ingest", RUNTIME_IMAGE, "/test/ingest-server", options=(
            "-v", f"{self.binary}:/test/ingest-server:ro", "--tmpfs", "/test/uploads",
            "-p", "127.0.0.1::50051", "-e", "STORAGE_ROOT=/test/uploads",
            "-e", f"S3_BUCKET={self.bucket}", "-e", "S3_ENDPOINT=http://objectstore:9000",
            "-e", "S3_REGION=us-east-1", "-e", "AWS_ACCESS_KEY_ID=robot-vault-ingest",
            "-e", "AWS_SECRET_ACCESS_KEY=resume-ingest-s3-password", "-e", "ENABLE_FILE_CAS=false",
            "-e", f"DATABASE_URL={self.ingest_database_url}",
            "-e", f"INGEST_AUTH_TOKEN={self.token}", "-e", "INGEST_STREAM_IDLE_TIMEOUT_SECONDS=10"))
        self.target = f"127.0.0.1:{self.port(self.ingest, 50051)}"
        with grpc.insecure_channel(self.target) as channel:
            try:
                grpc.channel_ready_future(channel).result(timeout=30)
            except Exception:
                raise AssertionError(run("docker", "logs", self.ingest))

    def restart_ingest(self):
        # SIGKILL and recreation discard all process memory and local tmpfs.
        run("docker", "rm", "-f", self.ingest)
        self.containers.remove(self.ingest)
        self.start_ingest()

    def close(self):
        for name in reversed(self.containers):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        subprocess.run(["docker", "network", "rm", self.network], capture_output=True, text=True)
        shutil.rmtree(self.secret_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def stack():
    binary = Path(BINARY).resolve()
    assert binary.is_file(), f"ingest binary not found: {binary}"
    stack = IsolatedIngest(binary)
    try:
        yield stack.start()
    finally:
        stack.close()


CLIENT_WORKER = r'''
import json, sys, time
from pathlib import Path
from robot_upload_agent.transport import GrpcUploadTransport
cfg = json.loads(Path(sys.argv[1]).read_text())
host, port = cfg['target'].rsplit(':', 1)
transport = GrpcUploadTransport(host, int(port), use_tls=False,
    auth_token=cfg['token'], robot_id='resume-test-robot', upload_rpc_timeout_seconds=90)
original_upload = transport.stub.Upload
sent = []
def pause_if_requested():
    if cfg.get('stop_after') is not None and len(sent) >= cfg['stop_after']:
        while True:
            time.sleep(0.1)
def observed_upload(requests, **kwargs):
    def observed():
        for request in requests:
            if request.HasField('chunk'):
                pause_if_requested()
                sent.append({'number': request.chunk.chunk_number, 'offset': request.chunk.offset, 'bytes': len(request.chunk.data)})
                Path(cfg['sent_path']).write_text(json.dumps(sent))
            yield request
        pause_if_requested()
    return original_upload(observed(), **kwargs)
transport.stub.Upload = observed_upload
try:
    result = transport.upload_stream(cfg['manifest'])
    Path(cfg['result_path']).write_text(json.dumps(result))
finally:
    transport.close()
'''


def start_client(stack, directory, manifest, stop_after=None):
    cfg = {"target": stack.target, "token": stack.token, "manifest": manifest,
           "stop_after": stop_after, "sent_path": str(directory / "sent.json"),
           "result_path": str(directory / "result.json")}
    directory.mkdir()
    config = directory / "worker.json"
    config.write_text(json.dumps(cfg))
    pythonpath = os.pathsep.join([str(ROOT / "phases/02-robot-upload-agent/src"),
                                str(ROOT / "phases/common-config/src"), os.environ.get("PYTHONPATH", "")])
    process = subprocess.Popen([sys.executable, "-c", CLIENT_WORKER, str(config)],
                               env={**os.environ, "PYTHONPATH": pythonpath}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return process, cfg


def finish_client(process):
    try:
        return process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=10)
        raise AssertionError(f"Client did not finish within 90 seconds: {stdout}{stderr}") from None


def manifest_file(directory, *, parts=10, digest=None):
    # Distinct deterministic parts let the object comparison catch holes,
    # duplicates and accidental part reordering. Five MiB meets S3's minimum.
    part_size = 5 * 1024 * 1024
    path = directory / f"recording-{uuid.uuid4().hex}.mcap"
    with path.open("wb") as source:
        for number in range(parts):
            source.write(bytes([number + 1]) * part_size)
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "upload_id": str(uuid.uuid4()), "file_size_bytes": path.stat().st_size,
            "sha256_hash": digest or sha256, "chunk_size_bytes": part_size, "compute_chunk_hashes": True}


def probe(stack, upload_id):
    import grpc
    import upload_pb2 as pb
    import upload_pb2_grpc as rpc
    with grpc.insecure_channel(stack.target) as channel:
        return rpc.UploadServiceStub(channel).ResumeUpload(
            pb.ResumeUploadRequest(upload_id=upload_id, resume_version=1),
            metadata=(("authorization", f"Bearer {stack.token}"),), timeout=10)


def wait_for_parts(stack, manifest, expected):
    import psycopg
    def durable_parts():
        with psycopg.connect(stack.database_url) as connection:
            count = connection.execute(
                "SELECT count(*) FROM upload_multipart_parts p JOIN uploads u ON u.id=p.upload_id WHERE u.client_upload_id=%s",
                (manifest["upload_id"],)).fetchone()[0]
        return count == expected
    return wait_until(durable_parts, f"{expected} committed multipart receipts", timeout=45)


def wait_for_s3_parts(stack, manifest, expected):
    def uploaded_parts():
        key, mpu_id = session_object(stack, manifest)
        parts = stack.s3.list_parts(Bucket=stack.bucket, Key=key, UploadId=mpu_id).get("Parts", [])
        return len(parts) == expected
    return wait_until(uploaded_parts, f"{expected} uploaded S3 parts", timeout=45)


def assert_stored(stack, manifest, result):
    import psycopg
    from urllib.parse import urlparse
    assert result["status"] == "completed"
    expected = Path(manifest["path"]).read_bytes()
    assert result["bytes_received"] == len(expected)
    assert result["final_hash"] == manifest["sha256_hash"]
    location = urlparse(result["storage_path"])
    key = location.path.lstrip("/") if location.scheme == "s3" else result["storage_path"]
    actual = stack.s3.get_object(Bucket=stack.bucket, Key=key)["Body"].read()
    assert actual == expected
    with psycopg.connect(stack.database_url) as connection:
        row = connection.execute("SELECT status, integrity_verified, bytes_received, sha256_hash FROM uploads WHERE client_upload_id = %s", (manifest["upload_id"],)).fetchone()
    assert row == ("completed", True, len(expected), manifest["sha256_hash"])


def test_resume_after_client_and_ingest_restart_sends_only_missing_part(stack, tmp_path):
    manifest = manifest_file(tmp_path)
    process, cfg = start_client(stack, tmp_path / "interrupted", manifest, stop_after=9)
    try:
        wait_for_s3_parts(stack, manifest, 9)
        assert process.poll() is None, process.communicate()
        # Kill both processes; no graceful flush or in-memory state survives.
        process.kill()
        process.communicate(timeout=10)
        wait_for_parts(stack, manifest, 9)
        stack.restart_ingest()
        resume = wait_until(lambda: probe(stack, manifest["upload_id"]), "resume probe")
        from tools.s3_gc import reachable_keys_from_pg, reclaim_incomplete_mpus
        snapshot = reachable_keys_from_pg(stack.gc_database_url, stack.bucket)
        assert snapshot.protected_mpus, "durable session must be protected from generic MPU GC"
        aborted, candidates = reclaim_incomplete_mpus(stack.gc_s3, stack.bucket,
            max_age_seconds=0, dry_run=False, prefix="raw/", protected_mpus=snapshot.protected_mpus)
        assert aborted == 0 and candidates == []
        assert resume.resume_version == 1
        assert resume.resume_session_id
        assert resume.chunk_size_bytes == manifest["chunk_size_bytes"]
        assert list(resume.missing_chunks) == [9]
        assert resume.bytes_received == 9 * manifest["chunk_size_bytes"]
        resumed, resumed_cfg = start_client(stack, tmp_path / "resumed", manifest)
        stdout, stderr = finish_client(resumed)
        assert resumed.returncode == 0, stdout + stderr
        sent = json.loads(Path(resumed_cfg["sent_path"]).read_text())
        assert sent == [{"number": 9, "offset": 9 * manifest["chunk_size_bytes"], "bytes": manifest["chunk_size_bytes"]}]
        assert_stored(stack, manifest, json.loads(Path(resumed_cfg["result_path"]).read_text()))
        print(f"Resume sent {sum(item['bytes'] for item in sent)} of {manifest['file_size_bytes']} bytes after both process restarts")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def interrupt_upload(stack, tmp_path, manifest, count):
    process, _ = start_client(stack, tmp_path / "interrupted", manifest, stop_after=count)
    try:
        wait_for_s3_parts(stack, manifest, count)
    finally:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == -9, stdout + stderr
    wait_for_parts(stack, manifest, count)
    stack.restart_ingest()


def test_resume_hole_uses_missing_set_not_total_bytes_as_prefix(stack, tmp_path):
    import psycopg
    manifest = manifest_file(tmp_path)
    interrupt_upload(stack, tmp_path, manifest, 9)
    # Simulate the crash window where S3 acknowledged a part but its receipt
    # transaction did not commit; later parts did commit. The orphan S3 part
    # must be overwritten rather than treated as an acknowledged prefix.
    with psycopg.connect(stack.database_url) as connection:
        connection.execute("DELETE FROM upload_multipart_parts WHERE upload_id=(SELECT id FROM uploads WHERE client_upload_id=%s) AND part_number=5", (manifest["upload_id"],))
    resume = probe(stack, manifest["upload_id"])
    assert list(resume.missing_chunks) == [4, 9]
    assert resume.bytes_received == 8 * manifest["chunk_size_bytes"]
    process, cfg = start_client(stack, tmp_path / "resumed", manifest)
    stdout, stderr = finish_client(process)
    assert process.returncode == 0, stdout + stderr
    sent = json.loads(Path(cfg["sent_path"]).read_text())
    assert [chunk["number"] for chunk in sent] == [4, 9]
    assert sum(chunk["bytes"] for chunk in sent) == 2 * manifest["chunk_size_bytes"]
    assert_stored(stack, manifest, json.loads(Path(cfg["result_path"]).read_text()))


def test_all_parts_saved_can_finalize_without_resending_payload(stack, tmp_path):
    manifest = manifest_file(tmp_path, parts=3)
    interrupt_upload(stack, tmp_path, manifest, 3)
    resume = probe(stack, manifest["upload_id"])
    assert resume.resume_version == 1 and resume.resume_session_id
    assert list(resume.missing_chunks) == []
    process, cfg = start_client(stack, tmp_path / "resumed", manifest)
    stdout, stderr = finish_client(process)
    assert process.returncode == 0, stdout + stderr
    sent_path = Path(cfg["sent_path"])
    assert not sent_path.exists() or json.loads(sent_path.read_text()) == []
    assert_stored(stack, manifest, json.loads(Path(cfg["result_path"]).read_text()))


def metadata_request(manifest, resume, **overrides):
    import upload_pb2 as pb
    fields = dict(robot_id="resume-test-robot", filename=Path(manifest["path"]).name,
                  file_size=manifest["file_size_bytes"], upload_id=manifest["upload_id"],
                  expected_sha256=manifest["sha256_hash"], content_type="application/octet-stream",
                  resume_version=1, chunk_size_bytes=resume.chunk_size_bytes,
                  resume_session_id=resume.resume_session_id)
    fields.update(overrides)
    return pb.UploadRequest(metadata=pb.UploadMetadata(**fields))


def raw_upload(stack, requests):
    import grpc
    import upload_pb2_grpc as rpc
    with grpc.insecure_channel(stack.target, options=[("grpc.max_send_message_length", 50 * 1024 * 1024)]) as channel:
        return rpc.UploadServiceStub(channel).Upload(iter(requests), timeout=30,
                                                    metadata=(("authorization", f"Bearer {stack.token}"),))


def test_stale_session_and_metadata_mismatch_do_not_consume_saved_parts(stack, tmp_path):
    import grpc
    manifest = manifest_file(tmp_path, parts=3)
    interrupt_upload(stack, tmp_path, manifest, 2)
    resume = probe(stack, manifest["upload_id"])
    with pytest.raises(grpc.RpcError) as stale:
        raw_upload(stack, [metadata_request(manifest, resume, resume_session_id=str(uuid.uuid4()))])
    assert stale.value.code() == grpc.StatusCode.ABORTED
    with pytest.raises(grpc.RpcError) as mismatch:
        raw_upload(stack, [metadata_request(manifest, resume, expected_sha256="f" * 64)])
    assert mismatch.value.code() in {grpc.StatusCode.ALREADY_EXISTS, grpc.StatusCode.FAILED_PRECONDITION}
    remaining = probe(stack, manifest["upload_id"])
    assert remaining.resume_session_id == resume.resume_session_id
    assert list(remaining.missing_chunks) == [2]
    process, cfg = start_client(stack, tmp_path / "resumed", manifest)
    stdout, stderr = finish_client(process)
    assert process.returncode == 0, stdout + stderr
    assert_stored(stack, manifest, json.loads(Path(cfg["result_path"]).read_text()))


def test_concurrent_resume_cannot_take_over_active_attempt(stack, tmp_path):
    import grpc
    import psycopg
    manifest = manifest_file(tmp_path, parts=3)
    interrupt_upload(stack, tmp_path, manifest, 2)
    resume = probe(stack, manifest["upload_id"])
    def generation():
        with psycopg.connect(stack.database_url) as connection:
            return connection.execute("SELECT attempt_generation FROM upload_multipart_sessions WHERE upload_id=(SELECT id FROM uploads WHERE client_upload_id=%s)", (manifest["upload_id"],)).fetchone()[0]
    previous_generation = generation()
    active, _ = start_client(stack, tmp_path / "active", manifest, stop_after=0)
    try:
        wait_until(lambda: generation() > previous_generation, "active attempt generation")
        with pytest.raises(grpc.RpcError) as concurrent:
            raw_upload(stack, [metadata_request(manifest, resume)])
        assert concurrent.value.code() in {grpc.StatusCode.ABORTED, grpc.StatusCode.RESOURCE_EXHAUSTED}
        wait_for_parts(stack, manifest, 2)
    finally:
        if active.poll() is None:
            active.kill()
        active.communicate(timeout=10)
    stack.restart_ingest()
    process, cfg = start_client(stack, tmp_path / "resumed", manifest)
    stdout, stderr = finish_client(process)
    assert process.returncode == 0, stdout + stderr
    assert_stored(stack, manifest, json.loads(Path(cfg["result_path"]).read_text()))


def test_expired_session_requires_full_resend(stack, tmp_path):
    import psycopg
    manifest = manifest_file(tmp_path, parts=3)
    interrupt_upload(stack, tmp_path, manifest, 2)
    previous = probe(stack, manifest["upload_id"])
    with psycopg.connect(stack.database_url) as connection:
        connection.execute("UPDATE upload_multipart_sessions SET expires_at=NOW()-INTERVAL '1 minute' WHERE upload_id=(SELECT id FROM uploads WHERE client_upload_id=%s)", (manifest["upload_id"],))
    expired = probe(stack, manifest["upload_id"])
    assert expired.bytes_received == 0
    assert not expired.resume_session_id
    process, cfg = start_client(stack, tmp_path / "resumed", manifest)
    stdout, stderr = finish_client(process)
    assert process.returncode == 0, stdout + stderr
    sent = json.loads(Path(cfg["sent_path"]).read_text())
    assert [chunk["number"] for chunk in sent] == [0, 1, 2]
    assert sum(chunk["bytes"] for chunk in sent) == manifest["file_size_bytes"]
    assert_stored(stack, manifest, json.loads(Path(cfg["result_path"]).read_text()))
    assert previous.resume_session_id


def test_final_hash_mismatch_never_publishes_verified_completion(stack, tmp_path):
    import psycopg
    manifest = manifest_file(tmp_path, parts=3, digest="f" * 64)
    # Save valid per-part hashes, then resume. The server must still verify
    # the whole assembled object against the immutable expected file digest.
    interrupt_upload(stack, tmp_path, manifest, 2)
    process, cfg = start_client(stack, tmp_path / "resumed", manifest)
    stdout, stderr = finish_client(process)
    result_path = Path(cfg["result_path"])
    if result_path.exists():
        assert json.loads(result_path.read_text())["status"] != "completed"
    else:
        assert process.returncode != 0, stdout + stderr
    with psycopg.connect(stack.database_url) as connection:
        row = connection.execute("SELECT status, integrity_verified FROM uploads WHERE client_upload_id=%s", (manifest["upload_id"],)).fetchone()
        sessions = connection.execute("SELECT count(*) FROM upload_multipart_sessions WHERE upload_id=(SELECT id FROM uploads WHERE client_upload_id=%s)", (manifest["upload_id"],)).fetchone()[0]
    assert row[0] == "failed"
    assert row[1] is not True
    assert sessions == 0


def session_object(stack, manifest):
    import psycopg
    from urllib.parse import urlparse
    with psycopg.connect(stack.database_url) as connection:
        key, mpu_id = connection.execute(
            "SELECT u.storage_path, s.s3_upload_id FROM upload_multipart_sessions s JOIN uploads u ON u.id=s.upload_id WHERE u.client_upload_id=%s",
            (manifest["upload_id"],)).fetchone()
    return urlparse(key).path.lstrip("/"), mpu_id


def test_missing_s3_multipart_session_recovers_with_full_resend(stack, tmp_path):
    manifest = manifest_file(tmp_path, parts=3)
    interrupt_upload(stack, tmp_path, manifest, 2)
    key, mpu_id = session_object(stack, manifest)
    # An object-store lifecycle rule or external cleanup removed the MPU
    # while its database receipts still exist.
    stack.s3.abort_multipart_upload(Bucket=stack.bucket, Key=key, UploadId=mpu_id)
    process, cfg = start_client(stack, tmp_path / "probe-orphan", manifest)
    stdout, stderr = finish_client(process)
    if process.returncode:
        assert "ABORTED" in stderr, stdout + stderr
        process, cfg = start_client(stack, tmp_path / "reset-orphan", manifest)
        stdout, stderr = finish_client(process)
        assert process.returncode == 0, stdout + stderr
    sent = json.loads(Path(cfg["sent_path"]).read_text())
    assert [chunk["number"] for chunk in sent] == [0, 1, 2]
    assert_stored(stack, manifest, json.loads(Path(cfg["result_path"]).read_text()))


def test_assembled_object_survives_crash_before_database_completion(stack, tmp_path):
    import psycopg
    manifest = manifest_file(tmp_path, parts=3)
    interrupt_upload(stack, tmp_path, manifest, 3)
    key, mpu_id = session_object(stack, manifest)
    with psycopg.connect(stack.database_url) as connection:
        upload_id = connection.execute("SELECT id FROM uploads WHERE client_upload_id=%s", (manifest["upload_id"],)).fetchone()[0]
        connection.execute("UPDATE upload_multipart_sessions SET state='finalizing' WHERE upload_id=%s", (upload_id,))
        rows = connection.execute("SELECT part_number, etag FROM upload_multipart_parts WHERE upload_id=%s ORDER BY part_number", (upload_id,)).fetchall()
    stack.s3.complete_multipart_upload(Bucket=stack.bucket, Key=key, UploadId=mpu_id,
        MultipartUpload={"Parts": [{"PartNumber": number, "ETag": etag} for number, etag in rows]})
    stack.restart_ingest()
    with psycopg.connect(stack.database_url) as connection:
        assert connection.execute("SELECT status FROM uploads WHERE id=%s", (upload_id,)).fetchone()[0] != "completed"
    process, cfg = start_client(stack, tmp_path / "finish-assembled", manifest)
    stdout, stderr = finish_client(process)
    assert process.returncode == 0, stdout + stderr
    sent_path = Path(cfg["sent_path"])
    assert not sent_path.exists() or json.loads(sent_path.read_text()) == []
    assert_stored(stack, manifest, json.loads(Path(cfg["result_path"]).read_text()))
