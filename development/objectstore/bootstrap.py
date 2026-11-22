"""One-shot object-store bootstrap: the release bucket and five least-privilege service users.

Runs once after the object store (RustFS) is healthy, as the `objectstore-access-ready`
one-shot. It talks to the RustFS admin REST API (`/rustfs/admin/v3`) with SigV4 (botocore,
already in the toolkit), so no admin CLI is needed and no secret is ever a command-line
argument: every secret comes from an environment variable or from a file named by
`<NAME>_FILE` (Compose secrets). See development/objectstore/CONTRACT.md.

Environment:
  S3_ENDPOINT                 store URL, e.g. http://objectstore:9000
  S3_BUCKET                   release bucket name
  S3_REGION                   default us-east-1
  OBJECTSTORE_POLICY_DIR      default /release/objectstore-policies
  OBJECTSTORE_ROOT_USER[_FILE], OBJECTSTORE_ROOT_PASSWORD[_FILE]
  <SVC>_S3_ACCESS_KEY, <SVC>_S3_SECRET_KEY[_FILE] for SVC in INGEST PROCESSOR EXPORTER QUERY GC

Idempotent: each run re-creates the five canned policies and re-creates each fixed user,
which clears stale policies and group memberships, then attaches exactly one policy and
reads it back. Rotate the secrets, not the fixed access-key identifiers.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

SERVICES = ("ingest", "processor", "exporter", "query", "gc")
BUCKET_MARKER = "__ROBOT_VAULT_BUCKET__"
ADMIN_PREFIX = "/rustfs/admin/v3"
TIMEOUT = 30


class BootstrapError(Exception):
    pass


def fail(message: str) -> None:
    raise BootstrapError(message)


def value(name: str, *, secret: bool = False) -> str:
    """Read NAME from the environment, or from the file named by NAME_FILE (not both)."""
    direct = os.environ.get(name)
    path = os.environ.get(f"{name}_FILE")
    if direct is not None and path is not None:
        fail(f"set only one of {name} and {name}_FILE")
    if path is not None:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"{name}_FILE is not readable: {exc.strerror}")
        raw = raw[:-1] if raw.endswith("\n") else raw
    else:
        raw = direct or ""
    if not raw:
        fail(f"{name} is required")
    if "\n" in raw or "\r" in raw:
        fail(f"{name} must not contain a line break")
    if secret and len(raw) < 16:
        fail(f"{name} must be at least 16 characters")
    return raw


def load_config() -> dict:
    endpoint = value("S3_ENDPOINT").rstrip("/")
    if urllib.parse.urlsplit(endpoint).scheme not in ("http", "https"):
        fail("S3_ENDPOINT must be an http or https URL")
    bucket = value("S3_BUCKET")
    if not (3 <= len(bucket) <= 63) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", bucket) or re.search(
        r"\.\.|\.-|-\.", bucket
    ):
        fail("S3_BUCKET is not a safe DNS-style bucket name")
    root_user = value("OBJECTSTORE_ROOT_USER")
    root_password = value("OBJECTSTORE_ROOT_PASSWORD", secret=True)
    users = {}
    secrets = {"OBJECTSTORE_ROOT_PASSWORD": root_password}
    for svc in SERVICES:
        up = svc.upper()
        access = value(f"{up}_S3_ACCESS_KEY")
        if access != f"robot-vault-{svc}":
            fail(f"{up}_S3_ACCESS_KEY must be the fixed identifier robot-vault-{svc}")
        if access == root_user:
            fail(f"{up}_S3_ACCESS_KEY must differ from OBJECTSTORE_ROOT_USER")
        secret = value(f"{up}_S3_SECRET_KEY", secret=True)
        for other, seen in secrets.items():
            if secret == seen:
                fail(f"{up}_S3_SECRET_KEY and {other} must differ")
        secrets[f"{up}_S3_SECRET_KEY"] = secret
        users[svc] = (access, secret)
    return {
        "endpoint": endpoint,
        "bucket": bucket,
        "region": os.environ.get("S3_REGION") or "us-east-1",
        "policy_dir": Path(os.environ.get("OBJECTSTORE_POLICY_DIR") or "/release/objectstore-policies"),
        "root": Credentials(root_user, root_password),
        "users": users,
    }


def call(cfg: dict, method: str, path: str, query: dict | None = None, body: bytes = b"") -> tuple[int, bytes]:
    """One SigV4-signed request with the root key. Returns (status, body); never logs the body."""
    qs = urllib.parse.urlencode(query or {}, quote_via=urllib.parse.quote)
    url = f"{cfg['endpoint']}{path}" + (f"?{qs}" if qs else "")
    req = AWSRequest(method=method, url=url, data=body, headers={"Content-Type": "application/json"} if body else {})
    S3SigV4Auth(cfg["root"], "s3", cfg["region"]).add_auth(req)
    prepared = urllib.request.Request(url, data=body or None, headers=dict(req.headers.items()), method=method)
    try:
        with urllib.request.urlopen(prepared, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def error_code(body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    match = re.search(r"<Code>([^<]+)</Code>", text) or re.search(r'"Code"\s*:\s*"([^"]+)"', text)
    return match.group(1) if match else text[:120]


def expect(status: int, body: bytes, what: str, ok=(200, 204)) -> bytes:
    if status not in ok:
        fail(f"{what} failed: HTTP {status} {error_code(body)}")
    return body


def wait_ready(cfg: dict, seconds: int = 120) -> None:
    last = "no answer"
    for _ in range(seconds):
        try:
            status, body = call(cfg, "GET", "/")
            if status == 200:
                return
            last = f"HTTP {status} {error_code(body)}"
        except OSError as exc:
            last = str(exc)
        time.sleep(1)
    fail(f"object store not ready after {seconds}s: {last}")


def ensure_bucket(cfg: dict) -> None:
    bucket = f"/{cfg['bucket']}"
    status, body = call(cfg, "PUT", bucket)
    if status not in (200, 204) and error_code(body) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
        fail(f"create bucket failed: HTTP {status} {error_code(body)}")
    # No anonymous access: the bucket must carry no bucket policy at all.
    status, body = call(cfg, "DELETE", bucket, {"policy": ""})
    if status not in (200, 204) and error_code(body) != "NoSuchBucketPolicy":
        fail(f"remove bucket policy failed: HTTP {status} {error_code(body)}")


def render_policy(cfg: dict, svc: str) -> bytes:
    source = cfg["policy_dir"] / f"{svc}.json"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        fail(f"missing policy template for {svc}")
    if BUCKET_MARKER not in text:
        fail(f"policy template for {svc} has no bucket marker")
    doc = json.loads(text.replace(BUCKET_MARKER, cfg["bucket"]))
    return json.dumps(doc, separators=(",", ":")).encode()


def provision(cfg: dict, svc: str) -> None:
    access, secret = cfg["users"][svc]
    policy = f"robot-vault-{svc}"
    status, body = call(cfg, "DELETE", f"{ADMIN_PREFIX}/remove-user", {"accessKey": access})
    if status not in (200, 204, 404) and "NoSuchUser" not in error_code(body):
        fail(f"remove user {access} failed: HTTP {status} {error_code(body)}")
    payload = json.dumps({"secretKey": secret, "status": "enabled"}).encode()
    expect(*call(cfg, "PUT", f"{ADMIN_PREFIX}/add-user", {"accessKey": access}, payload), f"add user {access}")
    expect(
        *call(
            cfg,
            "PUT",
            f"{ADMIN_PREFIX}/set-user-or-group-policy",
            {"policyName": policy, "userOrGroup": access, "isGroup": "false"},
        ),
        f"attach {policy} to {access}",
    )
    info = json.loads(expect(*call(cfg, "GET", f"{ADMIN_PREFIX}/user-info", {"accessKey": access}), f"user info {access}"))
    if info.get("policyName") != policy or info.get("status") != "enabled" or info.get("memberOf"):
        fail(
            f"user {access} read-back mismatch: policy={info.get('policyName')!r} "
            f"status={info.get('status')!r} groups={info.get('memberOf')!r}"
        )


def main() -> int:
    try:
        cfg = load_config()
        wait_ready(cfg)
        ensure_bucket(cfg)
        for svc in SERVICES:
            expect(
                *call(cfg, "PUT", f"{ADMIN_PREFIX}/add-canned-policy", {"name": f"robot-vault-{svc}"}, render_policy(cfg, svc)),
                f"create policy robot-vault-{svc}",
            )
        for svc in SERVICES:
            provision(cfg, svc)
    except BootstrapError as exc:
        print(f"objectstore bootstrap: {exc}", file=sys.stderr)
        return 1
    print("Object store release bucket and least-privilege service identities are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
