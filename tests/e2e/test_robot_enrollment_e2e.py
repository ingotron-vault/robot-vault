"""Robot enrollment E2E: one-use codes and per-robot credentials, end to end.

What this test does
-------------------
It starts a private, disposable stack in Docker: PostgreSQL, the object store (RustFS), the real
ingest server in the ``token`` security profile (the core default: TLS on, no
client certificate, shared token denied, provisioning listener on), the real
query service (development OIDC, provisioning proxy configured) and the real
processor. It then acts as an
administrator through the query admin API (``/api/admin/robots``) and as two
robots through the shipped robot agent (``ingotron_agent``).

The development stack is not used and not changed. Its ingest runs in the
``dev`` profile without a provisioning token, so it cannot issue per-robot
credentials. All containers, the network and the volumes have a unique
per-run prefix and are removed at the end.

Ways the system could fail, and the check that detects each
-------------------------------------------------------------
F1  The admin API is open to anyone or to a viewer. -> C01 (401, then 403).
F2  The query proxy cannot reach ingest provisioning (missing token, wrong
    upstream). -> C02/C03 need 201 replies with the documented fields.
F3  Ingest is not really in the token profile, so the shared operator
    token can still upload. -> C07 needs a refusal for the shared token.
F3b The vault tells robots the wrong ingest endpoint (the container port, or
    the query host). -> C03 needs vault_ingest_endpoint ingest:50051 in the
    mint reply and ``--ingest ingest:50051`` in the rendered install command;
    C04 enrolls at exactly that endpoint.
F4  Enrollment fails or issues a malformed credential, or a credential for
    the wrong robot. -> C04/C06 (exit 0, ``ignr1.`` length 58, mode 0600,
    vault.json names the robot and holds no credential, the admin API lists
    the robot as enrolled with exactly one credential).
F5  A one-use code can be used again. -> C05 (second use fails and no second
    credential appears for robot A).
F6  A per-robot credential cannot upload, or the upload is stored wrongly.
    -> C08 (agent run exits 0; the uploads row is robot A, completed and
    integrity-verified; its SHA-256 and the stored object's SHA-256
    equal the source file's; GetUploadStatus says completed).
F7  The upload is accepted but never processed. -> C09 (processing_status
    reaches completed and every Parquet topic row count is above zero).
F8  Robot A's credential can write or read as robot B. -> C10/C11 need
    PERMISSION_DENIED and no database row for the refused upload.
F9  A revoked credential or a disabled robot keeps working beyond the
    documented 30 s window. -> C12/C14 warm the credential cache
    first, act through the admin API, then measure the seconds to refusal.
F10 A refusal is caused by a broken environment, not by the security rule.
    -> every refusal check has a positive control: the same probe succeeds
    just before (warm-up), robot B still works after A is revoked (C13), and
    robot B works again after it is enabled (C15). Refusals must carry the
    exact gRPC code, not any error.
F11 Results leak between runs. -> fresh containers, volumes and robot ids on
    every run.

Run
---
    make test-robot-enrollment

or, after ``docker compose build dev ingest query processor objectstore``::

    ROBOT_ENROLLMENT_E2E=1 python3 -m pytest tests/e2e/test_robot_enrollment_e2e.py -v -s

The JSON report is written to ``tmp/robot-enrollment-e2e/<run id>/report.json``
and copied to ``tmp/robot-enrollment-e2e/latest.json``. It contains no tokens
or enrollment codes. Without ``ROBOT_ENROLLMENT_E2E=1`` the test is skipped,
so ``make test-e2e`` (which runs inside the dev container, without Docker)
does not try to start containers.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import pytest
except ImportError:  # allow `python3 tests/e2e/test_robot_enrollment_e2e.py`
    pytest = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[2]
ENABLED = os.environ.get("ROBOT_ENROLLMENT_E2E", "").strip().lower() in {"1", "true", "yes", "on"}
if pytest is not None:
    pytestmark = pytest.mark.skipif(
        not ENABLED, reason="set ROBOT_ENROLLMENT_E2E=1 (needs Docker); see module docstring"
    )

ARTIFACT_ROOT = ROOT / "tmp" / "robot-enrollment-e2e"
IMAGE_DEV = os.environ.get("ENROLLMENT_E2E_DEV_IMAGE", "robot-vault-dev:latest")
IMAGE_INGEST = os.environ.get("ENROLLMENT_E2E_INGEST_IMAGE", "robot-vault-ingest:latest")
IMAGE_QUERY = os.environ.get("ENROLLMENT_E2E_QUERY_IMAGE", "robot-vault-core-query:latest")
IMAGE_PROCESSOR = os.environ.get("ENROLLMENT_E2E_PROCESSOR_IMAGE", "robot-vault-core-processor:latest")
# The first-party store image (pinned RustFS, development/objectstore/CONTRACT.md section 1), built by docker-compose.yml.
IMAGE_OBJECTSTORE = os.environ.get("ENROLLMENT_E2E_OBJECTSTORE_IMAGE", "robot-vault-objectstore:latest")
IMAGE_POSTGRES = os.environ.get("ENROLLMENT_E2E_POSTGRES_IMAGE", "postgres:16-alpine")
IMAGES = [IMAGE_DEV, IMAGE_INGEST, IMAGE_QUERY, IMAGE_PROCESSOR, IMAGE_OBJECTSTORE, IMAGE_POSTGRES]

MCAP_FIXTURE = ROOT / "examples" / "wheeled-nominal.mcap"
BUCKET = "robotics-data"
ROOT_USER, ROOT_PASSWORD = "enroll-test-root", "enroll-test-root-password-01"
ROLES = ("ingest", "processor", "exporter", "query", "gc")
# The release permissions: fixed access keys, secrets only for this disposable stack.
S3_KEYS = {role: (f"robot-vault-{role}", f"enroll-{role}-s3-password") for role in ROLES}
DATABASE_URL = "postgres://robotvault:robotvault@postgres:5432/robotvault"
# Revocation or disable is effective for new RPCs within 30 s.
REVOCATION_WINDOW_SECONDS = 30.0
PROCESSING_TIMEOUT_SECONDS = 240.0
INGEST_ENDPOINT = "ingest:50051"   # INGEST_PUBLIC_HOST:INGEST_PUBLIC_PORT below
CODE_RE = re.compile(r"^ign1\.[0-9A-HJKMNP-TV-Z]{26}\.[0-9A-HJKMNP-TV-Z]{32}$")
REFUSAL_CODES = {"UNAUTHENTICATED", "PERMISSION_DENIED"}


def _run(args: List[str], *, input_text: Optional[str] = None, timeout: float = 300,
         check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args[:6])} ...\n"
                           f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}")
    return result


def _wait(check: Callable[[], Any], what: str, timeout: float, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
            last = value
        except Exception as exc:  # noqa: BLE001 - keep polling, report the last error
            last = exc
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}: {last!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Stack:
    """Disposable token-profile ingest, query and processor on a private network."""

    def __init__(self, run_dir: Path):
        self.prefix = f"rv-enroll-e2e-{uuid.uuid4().hex[:8]}"
        self.network = self.prefix
        self.containers: List[str] = []
        self.volumes: List[str] = []
        self.run_dir = run_dir
        self.certs = run_dir / "certs"
        self.ca_private = run_dir / "ca-private"
        self.auth_token = secrets.token_hex(32)
        self.provisioning_token = secrets.token_hex(32)
        self.query_url = ""
        self.network_created = False

    # -- Docker helpers -------------------------------------------------
    def _container(self, alias: str, image: str, *args: str, options: List[str] = ()) -> str:
        name = f"{self.prefix}-{alias}"
        self.containers.append(name)
        _run(["docker", "run", "-d", "--name", name, "--network", self.network,
              "--network-alias", alias, *options, image, *args])
        return name

    def volume(self, label: str) -> str:
        name = f"{self.prefix}-{label}"
        _run(["docker", "volume", "create", name])
        self.volumes.append(name)
        return name

    def run_dev(self, *args: str, mounts: Dict[str, str] = None, env: Dict[str, str] = None,
                input_text: Optional[str] = None, timeout: float = 300) -> subprocess.CompletedProcess:
        """Run one command in the dev image on the private network, as root."""
        options = ["docker", "run", "--rm", "-i", "--network", self.network, "-w", "/app",
                   "-v", f"{ROOT}:/app:ro", "-v", f"{self.certs}:/certs:ro"]
        for source, target in (mounts or {}).items():
            options += ["-v", f"{source}:{target}"]
        for key, value in (env or {}).items():
            options += ["-e", f"{key}={value}"]
        return _run([*options, IMAGE_DEV, *args], input_text=input_text, timeout=timeout, check=False)

    def psql(self, sql: str) -> str:
        return _run(["docker", "exec", f"{self.prefix}-postgres", "psql", "-U", "robotvault",
                     "-d", "robotvault", "-v", "ON_ERROR_STOP=1", "-Atc", sql]).stdout.strip()

    # -- Bring-up -------------------------------------------------------
    def _make_certs(self) -> None:
        self.certs.mkdir(parents=True)
        self.ca_private.mkdir(parents=True)
        ca_key, leaf = self.ca_private / "ca.key", self.certs
        _run(["openssl", "genrsa", "-out", str(ca_key), "3072"])
        _run(["openssl", "req", "-x509", "-new", "-nodes", "-key", str(ca_key), "-sha256",
              "-days", "2", "-subj", "/O=RobotVault/CN=enrollment-e2e-ca", "-out", str(leaf / "ca.crt")])
        _run(["openssl", "genrsa", "-out", str(leaf / "ingest.key"), "2048"])
        cnf = self.ca_private / "ingest.cnf"
        cnf.write_text(
            "[req]\ndistinguished_name=dn\nreq_extensions=v3\nprompt=no\n[dn]\nCN=ingest\n"
            "[v3]\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:ingest\n")
        csr = self.ca_private / "ingest.csr"
        _run(["openssl", "req", "-new", "-key", str(leaf / "ingest.key"), "-out", str(csr),
              "-config", str(cnf)])
        _run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(leaf / "ca.crt"),
              "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(leaf / "ingest.crt"),
              "-days", "2", "-sha256", "-extensions", "v3", "-extfile", str(cnf)])
        # Ingest runs as an unprivileged user; the key is disposable test material.
        os.chmod(leaf / "ingest.key", 0o644)

    def start(self) -> "Stack":
        self._make_certs()
        _run(["docker", "network", "create", self.network])
        self.network_created = True
        self._container("postgres", IMAGE_POSTGRES, options=[
            "--tmpfs", "/var/lib/postgresql/data", "-e", "POSTGRES_USER=robotvault",
            "-e", "POSTGRES_PASSWORD=robotvault", "-e", "POSTGRES_DB=robotvault"])
        # development/objectstore/CONTRACT.md section 2. `--user 0` only because the tmpfs data folder is owned by root.
        password_file = self.ca_private / "objectstore_root_password"
        password_file.write_text(ROOT_PASSWORD)
        password_file.chmod(0o644)
        settings = {"RUSTFS_ACCESS_KEY": ROOT_USER, "RUSTFS_SECRET_KEY_FILE": "/run/secrets/objectstore_root_password",
                    "RUSTFS_VOLUMES": "/data", "RUSTFS_ADDRESS": "0.0.0.0:9000", "RUSTFS_CONSOLE_ENABLE": "false",
                    "RUSTFS_DURABILITY_MODE": "strict", "RUSTFS_DRIVE_SYNC_ENABLE": "true",
                    "RUSTFS_NEW_BUCKET_DURABILITY_MODE": "strict", "RUSTFS_PUT_RENAME_EARLY_ACK_ENABLE": "false",
                    "RUSTFS_API_STALE_UPLOADS_EXPIRY": "360h", "RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL": "6h"}
        self._container("objectstore", IMAGE_OBJECTSTORE, options=[
            "--user", "0", "--tmpfs", "/data", "-v", f"{password_file}:/run/secrets/objectstore_root_password:ro",
            *[a for k, v in settings.items() for a in ("-e", f"{k}={v}")]])
        _wait(lambda: self.psql("SELECT 1") == "1", "PostgreSQL", 60)
        # The product bootstrap program makes the bucket and the five service keys (it waits for the store).
        boot_env = {"S3_ENDPOINT": "http://objectstore:9000", "S3_BUCKET": BUCKET, "S3_REGION": "us-east-1",
                    "OBJECTSTORE_ROOT_USER": ROOT_USER, "OBJECTSTORE_ROOT_PASSWORD": ROOT_PASSWORD,
                    "OBJECTSTORE_POLICY_DIR": "/app/development/release/objectstore-policies"}
        for role, (access, secret) in S3_KEYS.items():
            boot_env[f"{role.upper()}_S3_ACCESS_KEY"], boot_env[f"{role.upper()}_S3_SECRET_KEY"] = access, secret
        boot = self.run_dev("python", "development/objectstore/bootstrap.py", env=boot_env)
        if boot.returncode != 0:
            raise RuntimeError(f"object store bootstrap failed: {boot.stdout}\n{boot.stderr}")
        migrate = self.run_dev("python", "db/migrate.py", env={"DATABASE_URL": DATABASE_URL})
        if migrate.returncode != 0:
            raise RuntimeError(f"db/migrate.py failed: {migrate.stdout}\n{migrate.stderr}")

        # The release layout: one CA file that issued the ingest certificate.
        tls = "/run/robot-vault/tls"
        self._container("ingest", IMAGE_INGEST, options=[
            "-v", f"{self.certs / 'ingest.crt'}:{tls}/ingest.crt:ro",
            "-v", f"{self.certs / 'ingest.key'}:{tls}/ingest.key:ro",
            "-v", f"{self.certs / 'ca.crt'}:{tls}/ingest-ca.pem:ro",
            "-e", f"DATABASE_URL={DATABASE_URL}", "-e", "STORAGE_ROOT=/tmp/uploads",
            "-e", f"S3_BUCKET={BUCKET}", "-e", "S3_ENDPOINT=http://objectstore:9000",
            "-e", "S3_REGION=us-east-1", "-e", f"AWS_ACCESS_KEY_ID={S3_KEYS['ingest'][0]}",
            "-e", f"AWS_SECRET_ACCESS_KEY={S3_KEYS['ingest'][1]}", "-e", "ENABLE_FILE_CAS=true",
            "-e", "INGEST_SECURITY_PROFILE=token",
            "-e", f"INGEST_AUTH_TOKEN={self.auth_token}",
            "-e", f"INGEST_PROVISIONING_TOKEN={self.provisioning_token}",
            "-e", "TLS_ENABLED=true",
            "-e", f"TLS_SERVER_CERT={tls}/ingest.crt",
            "-e", f"TLS_SERVER_KEY={tls}/ingest.key",
            "-e", f"TLS_CA_CERT={tls}/ingest-ca.pem",
            "-e", "INGEST_PUBLIC_HOST=ingest", "-e", "INGEST_PUBLIC_PORT=50051",
            "-e", "INGEST_ROBOT_SELF_ROTATION=disabled"])
        _wait(lambda: _run(["docker", "exec", f"{self.prefix}-postgres", "wget", "-q", "-O", "/dev/null", "-S",
                            "http://ingest:8080/readyz"], check=False).stderr.find(" 200 ") >= 0,
              "ingest /readyz", 90)

        self._container("query", IMAGE_QUERY, options=[
            "-p", "127.0.0.1::8003", "--env-file", str(ROOT / "configs" / "query.env"),
            "-e", f"AWS_ACCESS_KEY_ID={S3_KEYS['query'][0]}", "-e", f"AWS_SECRET_ACCESS_KEY={S3_KEYS['query'][1]}",
            "-e", f"DATABASE_URL={DATABASE_URL}", "-e", "REDIS_URL=",
            "-e", "ROBOT_VAULT_EDITION=core",
            "-e", f"INGEST_PROVISIONING_TOKEN={self.provisioning_token}",
            "-e", "INGEST_PROVISIONING_UPSTREAM=http://ingest:8080",
            # The public origin robots fetch /robot from; the install command needs it.
            "-e", "PUBLIC_BASE_URL=https://query.rv-test.invalid"])
        port = _run(["docker", "port", f"{self.prefix}-query", "8003/tcp"]).stdout.split()[0].rsplit(":", 1)[1]
        self.query_url = f"http://127.0.0.1:{port}"
        _wait(lambda: urllib.request.urlopen(self.query_url + "/healthz", timeout=3).status == 200,
              "query /healthz", 90)

        self._container("processor", IMAGE_PROCESSOR, "python3", "-m", "processor.cli", "--watch",
                        "--interval", "2", options=[
            "--env-file", str(ROOT / "configs" / "processor.env"),
            "-e", f"AWS_ACCESS_KEY_ID={S3_KEYS['processor'][0]}", "-e", f"AWS_SECRET_ACCESS_KEY={S3_KEYS['processor'][1]}",
            "-e", f"DATABASE_URL={DATABASE_URL}", "-e", "ROBOT_VAULT_EDITION=core",
            "-e", "QA_ENABLED=false", "-e", "MEDIA_ENCODE_ENABLED=false", "-e", "PII_BACKEND=none"])
        return self

    def object_sha256(self, key: str) -> str:
        """SHA-256 of a stored object, read with the ingest key (it has GetObject on raw/)."""
        code = ("import boto3, hashlib, os\nfrom botocore.config import Config\n"
                "c = boto3.client('s3', endpoint_url='http://objectstore:9000', region_name='us-east-1',"
                " aws_access_key_id=os.environ['K'], aws_secret_access_key=os.environ['S'],"
                " config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}))\n"
                "h = hashlib.sha256()\n"
                "for chunk in c.get_object(Bucket=os.environ['B'], Key=os.environ['G'])['Body'].iter_chunks():\n"
                "    h.update(chunk)\nprint(h.hexdigest())\n")
        result = self.run_dev("python", "-c", code, env={"K": S3_KEYS["ingest"][0], "S": S3_KEYS["ingest"][1],
                                                         "B": BUCKET, "G": key})
        return result.stdout.strip() if result.returncode == 0 else ""

    def logs(self) -> Dict[str, str]:
        out = {}
        for name in self.containers:
            result = _run(["docker", "logs", "--tail", "60", name], check=False)
            out[name] = (result.stdout + result.stderr)[-6000:]
        return out

    def close(self) -> Dict[str, Any]:
        removed = {"containers": [], "volumes": [], "network": None, "errors": []}
        for name in reversed(self.containers):
            r = _run(["docker", "rm", "-fv", name], check=False)
            (removed["containers"] if r.returncode == 0 else removed["errors"]).append(name)
        for name in self.volumes:
            r = _run(["docker", "volume", "rm", "-f", name], check=False)
            (removed["volumes"] if r.returncode == 0 else removed["errors"]).append(name)
        if self.network_created:
            r = _run(["docker", "network", "rm", self.network], check=False)
            removed["network"] = self.network if r.returncode == 0 else None
            if r.returncode != 0:
                removed["errors"].append(self.network)
        # The CA private key and the TLS key are disposable, but do not leave them.
        shutil.rmtree(self.certs, ignore_errors=True)
        shutil.rmtree(self.ca_private, ignore_errors=True)
        return removed


class Admin:
    """The query admin API, called as a browser administrator would."""

    def __init__(self, base: str):
        self.base = base
        self.token = self.dev_token("admin")

    def dev_token(self, roles: str) -> str:
        with urllib.request.urlopen(f"{self.base}/auth/dev/token?roles={roles}", timeout=10) as r:
            return json.loads(r.read())["access_token"]

    def call(self, method: str, path: str, body: Optional[dict] = None,
             token: Optional[str] = "default") -> tuple[int, Any]:
        token = self.token if token == "default" else token
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                status, raw = reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")

    def robot(self, robot_id: str) -> Optional[dict]:
        status, body = self.call("GET", "/api/admin/robots/lifecycle")
        assert status == 200, f"lifecycle list failed: {status} {body}"
        return next((r for r in body["robots"] if r["robot_id"] == robot_id), None)


class Report:
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []

    def check(self, check_id: str, title: str, passed: bool, evidence: Any) -> bool:
        self.checks.append({"id": check_id, "title": title, "passed": bool(passed), "evidence": evidence})
        print(f"[{'PASS' if passed else 'FAIL'}] {check_id} {title}", flush=True)
        return bool(passed)


def _last_json(stdout: str) -> Dict[str, Any]:
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def run_scenario(stack: Stack, report: Report) -> Dict[str, Any]:
    facts: Dict[str, Any] = {}
    run_tag = stack.prefix.rsplit("-", 1)[-1]
    robot_a, robot_b = f"e2e-a-{run_tag}", f"e2e-b-{run_tag}"
    facts["robots"] = {"a": robot_a, "b": robot_b}
    admin = Admin(stack.query_url)

    sandboxes = {name: stack.volume(f"sandbox-{name}") for name in ("a", "b", "reuse")}
    recordings = {name: stack.volume(f"rec-{name}") for name in ("a", "b", "reuse")}
    agent_env = {"INGOTRON_ROOT_PREFIX": "/sandbox",
                 "PYTHONPATH": "/opt/proto-gen:/app/packaging/robot-agent/agent/lib"}

    def agent(name: str, *args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        return stack.run_dev("python3", *args, input_text=input_text, env=agent_env,
                             mounts={sandboxes[name]: "/sandbox", recordings[name]: "/recordings"})

    def probe(name: str, *args: str, env: Dict[str, str] = None) -> Dict[str, Any]:
        result = stack.run_dev("python3", "development/enrollment-e2e/probe.py", *args,
                               env={**(env or {}), "PYTHONPATH": "/opt/proto-gen"},
                               mounts={sandboxes[name]: "/sandbox"})
        if result.returncode != 0:
            return {"ok": False, "code": "PROBE_ERROR", "details": result.stderr[-1500:]}
        return _last_json(result.stdout)

    token_file = "/sandbox/etc/ingotron/credentials/robot.token"

    # C01: the admin API is admin-only.
    anon, _ = admin.call("POST", "/api/admin/robots", {"robot_id": robot_a}, token=None)
    viewer, _ = admin.call("POST", "/api/admin/robots", {"robot_id": robot_a},
                           token=admin.dev_token("viewer"))
    report.check("C01", "query admin API refuses anonymous (401) and viewer (403) callers",
                 anon == 401 and viewer == 403, {"anonymous": anon, "viewer": viewer})

    # C02/C03: create both robots and mint one code each, through query.
    created, codes = {}, {}
    for key, robot in (("a", robot_a), ("b", robot_b)):
        created[key] = admin.call("POST", "/api/admin/robots", {"robot_id": robot, "name": f"E2E {key}"})
    report.check("C02", "POST /api/admin/robots creates pending per_robot robots through the ingest proxy",
                 all(s == 201 and b.get("state") == "pending" and b.get("auth_mode") == "per_robot"
                     for s, b in created.values()),
                 {k: {"status": s, "body": b} for k, (s, b) in created.items()})
    minted, endpoints = {}, {}
    for key, robot in (("a", robot_a), ("b", robot_b)):
        status, body = admin.call("POST", f"/api/admin/robots/{robot}/enrollment-code",
                                  {"ttl_seconds": 900, "purpose": "enroll"})
        codes[key] = body.get("code", "") if isinstance(body, dict) else ""
        body = body if isinstance(body, dict) else {}
        endpoints[key] = body.get("vault_ingest_endpoint", "")
        minted[key] = {"status": status, "code_shape_ok": bool(CODE_RE.match(codes[key])),
                       "install_command_present": "install_command" in body,
                       "vault_ingest_endpoint": endpoints[key],
                       "command_names_the_endpoint": f"--ingest {INGEST_ENDPOINT} " in body.get("install_command", ""),
                       "ca_spki_sha256": body.get("ca_spki_sha256")}
    if not report.check("C03", "POST .../enrollment-code mints a one-use ign1 code and names the ingest endpoint",
                        all(m["status"] == 201 and m["code_shape_ok"] and m["install_command_present"]
                            and m["vault_ingest_endpoint"] == INGEST_ENDPOINT and m["command_names_the_endpoint"]
                            for m in minted.values()), minted):
        return facts

    # C04: robot A enrolls with the shipped agent, the code read from stdin.
    # The robot enrolls at the endpoint the vault named, not at <vault host>:50051.
    enroll_args = ["-m", "ingotron_agent.cli", "enroll", "--vault", "https://query.rv-test.invalid",
                   "--ingest", endpoints["a"], "--code-stdin", "--ca", "/certs/ca.crt", "--root", "/recordings"]
    enroll_a = agent("a", *enroll_args, input_text=codes["a"] + "\n")
    inspect_a = agent("a", "-c", (
        "import json,pathlib;t=pathlib.Path('/sandbox/etc/ingotron/credentials/robot.token');"
        "v=json.loads(pathlib.Path('/sandbox/etc/ingotron/vault.json').read_text());"
        "s=t.read_text().strip() if t.exists() else '';"
        "print(json.dumps({'prefix':s[:6],'length':len(s),'mode':oct(t.stat().st_mode&0o777) if t.exists() else None,"
        "'robot_id':v.get('robot_id'),'inline_token':bool((v.get('auth') or {}).get('token')),"
        "'credential_in_vault_json':'ignr1.' in json.dumps(v)}))"))
    seen_a = _last_json(inspect_a.stdout) if inspect_a.returncode == 0 else {"error": inspect_a.stderr[-800:]}
    listed_a = admin.robot(robot_a) or {}
    creds_a = listed_a.get("credentials", [])
    report.check("C04", "robot A enrolls with the agent and gets its own ignr1 credential",
                 enroll_a.returncode == 0 and seen_a.get("prefix") == "ignr1." and seen_a.get("length") == 58
                 and seen_a.get("mode") == "0o600" and seen_a.get("robot_id") == robot_a
                 and not seen_a.get("inline_token") and not seen_a.get("credential_in_vault_json")
                 and listed_a.get("state") == "enrolled" and len(creds_a) == 1,
                 {"enroll_exit": enroll_a.returncode, "enroll_output": (enroll_a.stdout + enroll_a.stderr)[-600:],
                  "robot_side": seen_a, "admin_view": {"state": listed_a.get("state"),
                  "credentials": [{k: c.get(k) for k in ("fingerprint", "issued_via", "revoked_at")} for c in creds_a]}})

    # C05: the same code cannot be used again (fresh robot host, same code).
    reuse = agent("reuse", *enroll_args, input_text=codes["a"] + "\n")
    listed_a_after = admin.robot(robot_a) or {}
    reuse_text = (reuse.stdout + reuse.stderr)
    report.check("C05", "a used enrollment code is refused and issues no second credential",
                 reuse.returncode != 0 and "invalid or expired enrollment code" in reuse_text.lower()
                 and len(listed_a_after.get("credentials", [])) == 1,
                 {"exit": reuse.returncode, "output": reuse_text[-400:],
                  "credentials_for_a": len(listed_a_after.get("credentials", []))})

    enroll_b = agent("b", *enroll_args, input_text=codes["b"] + "\n")
    listed_b = admin.robot(robot_b) or {}
    report.check("C06", "robot B enrolls with its own code",
                 enroll_b.returncode == 0 and listed_b.get("state") == "enrolled"
                 and len(listed_b.get("credentials", [])) == 1,
                 {"exit": enroll_b.returncode, "state": listed_b.get("state")})

    # C07: token profile: the shared operator token must not upload.
    shared = probe("a", "upload", "--token-env", "SHARED_TOKEN", "--robot-id", robot_a,
                   env={"SHARED_TOKEN": stack.auth_token})
    report.check("C07", "token profile refuses an upload with the shared operator token",
                 not shared.get("ok") and shared.get("code") in REFUSAL_CODES,
                 {k: shared.get(k) for k in ("ok", "code", "details")})

    # C08: robot A's agent uploads a real MCAP with its credential.
    source_sha = _sha256_file(MCAP_FIXTURE)
    copy = stack.run_dev("sh", "-ec", "cp /app/examples/wheeled-nominal.mcap /recordings/episode_a.mcap && "
                         "touch -d '@'$(( $(date +%s) - 300 )) /recordings/episode_a.mcap",
                         mounts={recordings["a"]: "/recordings"})
    run_a = agent("a", "-m", "ingotron_agent.runner", "--config", "/sandbox/etc/ingotron/vault.json")
    row = stack.psql(
        "SELECT row_to_json(u) FROM (SELECT id, client_upload_id, robot_id, status, integrity_verified, sha256_hash, "
        "file_size_bytes, storage_path FROM uploads WHERE original_filename='episode_a.mcap') u")
    upload = json.loads(row) if row else {}
    object_sha = ""
    if upload.get("storage_path"):
        key = re.sub(r"^s3://[^/]+/", "", upload["storage_path"]).lstrip("/")
        object_sha = stack.object_sha256(key)
    # GetUploadStatus is keyed by the client upload id the agent generated.
    status_a = probe("a", "status", "--token-file", token_file, "--upload-id",
                     str(upload.get("client_upload_id") or ""))
    report.check("C08", "robot A's agent uploads a real MCAP; row, object hash and status agree",
                 copy.returncode == 0 and run_a.returncode == 0 and upload.get("robot_id") == robot_a
                 and upload.get("status") == "completed" and upload.get("integrity_verified") is True
                 and upload.get("sha256_hash") == source_sha == object_sha
                 and status_a.get("status") == "completed",
                 {"runner_exit": run_a.returncode, "runner_tail": (run_a.stdout + run_a.stderr)[-600:],
                  "source_sha256": source_sha, "uploads_row": upload, "object_sha256": object_sha,
                  "get_upload_status": status_a})
    facts["upload_a"] = upload.get("id")

    # C09: the processor turns that upload into processed data.
    processing = None
    if upload.get("id"):
        try:
            processing = _wait(lambda: (lambda s: s if s in {"completed", "failed"} else None)(
                stack.psql(f"SELECT status FROM processing_status WHERE upload_id='{upload['id']}'")),
                "processing_status terminal", PROCESSING_TIMEOUT_SECONDS, 2)
        except AssertionError as exc:
            processing = str(exc)
    topics = json.loads(stack.psql(
        "SELECT coalesce(json_agg(json_build_object('topic', topic, 'rows', rows_written)), '[]') "
        f"FROM processed_topics WHERE upload_id='{upload['id']}'")) if upload.get("id") else []
    report.check("C09", "the enrolled robot's upload is processed into Parquet topics",
                 processing == "completed" and len(topics) > 0 and all(t["rows"] > 0 for t in topics),
                 {"processing_status": processing, "processed_topics": topics})

    # B's own upload, used by the cross-robot read check.
    upload_b = probe("b", "upload", "--token-file", token_file, "--robot-id", robot_b)

    # C10: A's credential claiming robot B is refused, and nothing is stored.
    cross = probe("a", "upload", "--token-file", token_file, "--robot-id", robot_b)
    stored = stack.psql(f"SELECT count(*) FROM uploads WHERE client_upload_id='{cross.get('client_upload_id')}'")
    report.check("C10", "robot A's credential cannot upload as robot B",
                 upload_b.get("ok") and not cross.get("ok") and cross.get("code") == "PERMISSION_DENIED"
                 and stored == "0",
                 {"control_b_upload": {k: upload_b.get(k) for k in ("ok", "code", "status")},
                  "cross_upload": {k: cross.get(k) for k in ("ok", "code", "details")},
                  "rows_for_refused_upload": stored})

    # C11: A cannot read B's uploads, and an unfiltered list shows only A.
    read_b = probe("a", "status", "--token-file", token_file, "--upload-id", str(upload_b.get("upload_id", "")))
    list_b = probe("a", "list", "--token-file", token_file, "--filter", robot_b)
    list_own = probe("a", "list", "--token-file", token_file)
    own_robots = sorted({u["robot_id"] for u in list_own.get("uploads", [])})
    report.check("C11", "robot A's credential cannot read robot B's uploads",
                 read_b.get("code") == "PERMISSION_DENIED" and list_b.get("code") == "PERMISSION_DENIED"
                 and list_own.get("ok") and own_robots == [robot_a],
                 {"get_status_of_b": {k: read_b.get(k) for k in ("code", "details")},
                  "list_filter_b": {k: list_b.get(k) for k in ("code", "details")},
                  "unfiltered_list_robot_ids": own_robots, "unfiltered_list_count": len(list_own.get("uploads", []))})

    # C12: revoke A's credential through the admin API; measure time to refusal.
    admin_file_setup = stack.run_dev("sh", "-ec", "cat > /sandbox/admin.token && chmod 600 /sandbox/admin.token",
                                     input_text=admin.token, mounts={sandboxes["a"]: "/sandbox"})
    stack.run_dev("sh", "-ec", "cat > /sandbox/admin.token && chmod 600 /sandbox/admin.token",
                  input_text=admin.token, mounts={sandboxes["b"]: "/sandbox"})
    credential_id = (creds_a[0]["credential_id"] if creds_a else "missing")
    revoke = probe("a", "refuse-after-action", "--token-file", token_file, "--robot-id", robot_a,
                   "--admin-token-file", "/sandbox/admin.token",
                   "--action-url", f"http://query:8003/api/admin/robots/{robot_a}/credentials/{credential_id}/revoke",
                   "--action-body", json.dumps({"reason": "robot enrollment E2E"}),
                   "--window", str(REVOCATION_WINDOW_SECONDS))
    report.check("C12", f"a revoked credential is refused within {REVOCATION_WINDOW_SECONDS:.0f} s",
                 admin_file_setup.returncode == 0 and revoke.get("stage") == "done"
                 and revoke.get("warm_up", {}).get("ok") and revoke.get("refused")
                 and revoke.get("refusal_code") in REFUSAL_CODES
                 and revoke.get("seconds_to_refusal", 1e9) <= REVOCATION_WINDOW_SECONDS,
                 {k: revoke.get(k) for k in ("stage", "action_status", "accepted_after_action", "refused",
                                             "refusal_code", "refusal_details", "seconds_to_refusal")}
                 | {"warm_up_ok": revoke.get("warm_up", {}).get("ok")})

    # C13: revoking A did not affect B.
    b_after = probe("b", "upload", "--token-file", token_file, "--robot-id", robot_b)
    report.check("C13", "robot B still uploads after robot A is revoked",
                 b_after.get("ok") and b_after.get("status") == "completed",
                 {k: b_after.get(k) for k in ("ok", "code", "status")})

    # C14: disable robot B through the admin API; measure time to refusal.
    disable = probe("b", "refuse-after-action", "--token-file", token_file, "--robot-id", robot_b,
                    "--admin-token-file", "/sandbox/admin.token",
                    "--action-url", f"http://query:8003/api/admin/robots/{robot_b}/disable",
                    "--action-body", json.dumps({"reason": "robot enrollment E2E"}),
                    "--window", str(REVOCATION_WINDOW_SECONDS))
    report.check("C14", f"a disabled robot is refused within {REVOCATION_WINDOW_SECONDS:.0f} s",
                 disable.get("stage") == "done" and disable.get("warm_up", {}).get("ok")
                 and disable.get("refused") and disable.get("refusal_code") in REFUSAL_CODES
                 and disable.get("seconds_to_refusal", 1e9) <= REVOCATION_WINDOW_SECONDS,
                 {k: disable.get(k) for k in ("stage", "action_status", "accepted_after_action", "refused",
                                              "refusal_code", "refusal_details", "seconds_to_refusal")}
                 | {"warm_up_ok": disable.get("warm_up", {}).get("ok")})

    # C15: enabling B restores it; the refusal was the robot state, not the environment.
    enable_status, enable_body = admin.call("POST", f"/api/admin/robots/{robot_b}/enable", {"reason": "E2E"})
    b_enabled = _wait(lambda: (lambda r: r if r.get("ok") else None)(
        probe("b", "upload", "--token-file", token_file, "--robot-id", robot_b)),
        "robot B upload after enable", REVOCATION_WINDOW_SECONDS + 5, 1)
    report.check("C15", "enabling robot B lets the same credential upload again",
                 enable_status == 200 and b_enabled.get("status") == "completed",
                 {"enable_status": enable_status, "enable_body": enable_body,
                  "upload": {k: b_enabled.get(k) for k in ("ok", "code", "status")}})

    facts["final_admin_view"] = {
        key: {"state": (r := admin.robot(robot) or {}).get("state"),
              "credentials": [{k: c.get(k) for k in ("fingerprint", "revoked_at")} for c in r.get("credentials", [])]}
        for key, robot in (("a", robot_a), ("b", robot_b))}
    facts["uploads_by_robot"] = stack.psql(
        "SELECT coalesce(json_object_agg(robot_id, n), '{}') FROM "
        "(SELECT robot_id, count(*) n FROM uploads GROUP BY robot_id) s")
    return facts


def _image_ids() -> Dict[str, Any]:
    out = {}
    for image in IMAGES:
        r = _run(["docker", "image", "inspect", image, "--format", "{{json .}}"], check=False)
        if r.returncode != 0:
            out[image] = None
            continue
        meta = json.loads(r.stdout)[0] if r.stdout.strip().startswith("[") else json.loads(r.stdout)
        labels = (meta.get("Config") or {}).get("Labels") or {}
        out[image] = {"id": meta.get("Id"), "revision": labels.get("org.opencontainers.image.revision")}
    return out


def execute() -> Dict[str, Any]:
    started = _dt.datetime.now(_dt.timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_dir = ARTIFACT_ROOT / run_id
    run_dir.mkdir(parents=True)
    report = Report()
    images = _image_ids()
    missing = [image for image, value in images.items() if not value]
    result: Dict[str, Any] = {
        "test": "tests/e2e/test_robot_enrollment_e2e.py", "run_id": run_id,
        "started_at": started.isoformat(), "git_head": _run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                                           check=False).stdout.strip(),
        "git_dirty": bool(_run(["git", "-C", str(ROOT), "status", "--porcelain"], check=False).stdout.strip()),
        "images": images, "revocation_window_seconds": REVOCATION_WINDOW_SECONDS,
    }
    stack = Stack(run_dir)
    try:
        if missing:
            raise RuntimeError(f"missing images: {missing}; run docker compose build dev ingest query processor objectstore")
        stack.start()
        result["facts"] = run_scenario(stack, report)
    except Exception as exc:  # noqa: BLE001 - record and fail below
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["container_logs"] = stack.logs()
    finally:
        if any(not c["passed"] for c in report.checks) and "container_logs" not in result:
            result["container_logs"] = stack.logs()
        result["cleanup"] = stack.close()
    result["checks"] = report.checks
    result["finished_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    result["passed"] = ("error" not in result and len(report.checks) == 15
                        and all(c["passed"] for c in report.checks) and not result["cleanup"]["errors"])
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    for secret in (stack.auth_token, stack.provisioning_token):
        assert secret not in text, "a token reached the report"
    (run_dir / "report.json").write_text(text + "\n")
    (ARTIFACT_ROOT / "latest.json").write_text(text + "\n")
    print(f"report: {run_dir / 'report.json'} passed={result['passed']}", flush=True)
    return result


def test_robot_enrollment_end_to_end() -> None:
    result = execute()
    failed = [f"{c['id']} {c['title']}" for c in result["checks"] if not c["passed"]]
    assert result["passed"], (
        f"error={result.get('error')}; failed checks={failed}; "
        f"checks run={len(result['checks'])}/15; cleanup errors={result['cleanup']['errors']}; "
        f"see tmp/robot-enrollment-e2e/{result['run_id']}/report.json")


if __name__ == "__main__":
    sys.exit(0 if execute()["passed"] else 1)
