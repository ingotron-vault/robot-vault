"""gRPC and admin-API probe for tests/e2e/test_robot_enrollment_e2e.py.

The E2E test runs this script inside the `robot-vault-dev` image, on the
test's private Docker network, so it can reach `ingest:50051` (TLS) and
`query:8003`. Every command prints exactly one JSON object on the last line
of stdout. A refused RPC is a result, not a script error: the JSON carries
the gRPC status code so the test can require the exact refusal it expects.

Commands:

  list    --token-file F [--filter ROBOT]       ListUploads
  status  --token-file F --upload-id U          GetUploadStatus
  upload  --token-file F --robot-id R           UploadSmall with fresh random bytes
  refuse-after-action --token-file F --robot-id R --action-url URL
          --admin-token-file A [--action-body JSON] [--window S]
      Upload once (must succeed, so the credential cache is warm), POST the
      admin action, then upload repeatedly until ingest refuses. Reports the
      seconds from the admin reply to the first refusal.

Use `--token-env NAME` in place of `--token-file` to send a token from the
environment (the shared operator token check).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import grpc

import upload_pb2 as pb
import upload_pb2_grpc as pb_grpc

INGEST_TARGET = os.environ.get("PROBE_INGEST_TARGET", "ingest:50051")
CA_PATH = os.environ.get("PROBE_CA_PATH", "/certs/ca.crt")


def _token(args: argparse.Namespace) -> str:
    if args.token_env:
        return os.environ[args.token_env].strip()
    return Path(args.token_file).read_text(encoding="ascii").strip()


def _stub() -> tuple[grpc.Channel, pb_grpc.UploadServiceStub]:
    creds = grpc.ssl_channel_credentials(root_certificates=Path(CA_PATH).read_bytes())
    channel = grpc.secure_channel(INGEST_TARGET, creds)
    return channel, pb_grpc.UploadServiceStub(channel)


def _call(fn) -> dict:
    try:
        return {"ok": True, "code": "OK", "details": "", **fn()}
    except grpc.RpcError as exc:
        return {"ok": False, "code": exc.code().name, "details": exc.details() or ""}


def op_list(stub, metadata, args) -> dict:
    def go():
        resp = stub.ListUploads(
            pb.ListUploadsRequest(robot_id=args.filter or "", page_size=100),
            metadata=metadata, timeout=15,
        )
        return {"uploads": [{"upload_id": u.upload_id, "robot_id": u.robot_id,
                             "filename": u.filename, "status": u.status}
                            for u in resp.uploads]}
    return _call(go)


def op_status(stub, metadata, args) -> dict:
    def go():
        resp = stub.GetUploadStatus(pb.UploadStatusRequest(upload_id=args.upload_id),
                                    metadata=metadata, timeout=15)
        return {"upload_id": resp.upload_id, "status": resp.status,
                "bytes_received": resp.bytes_received, "total_bytes": resp.total_bytes}
    return _call(go)


def op_upload(stub, metadata, robot_id: str) -> dict:
    # Fresh random bytes each time: content-addressed deduplication must never
    # turn a refused credential into an apparent success.
    data = os.urandom(2048)
    digest = hashlib.sha256(data).hexdigest()
    client_id = str(uuid.uuid4())

    def go():
        md = pb.UploadMetadata(robot_id=robot_id, filename=f"probe-{client_id}.bin",
                               file_size=len(data), content_type="application/octet-stream",
                               upload_id=client_id, expected_sha256=digest)
        resp = stub.UploadSmall(pb.UploadSmallRequest(metadata=md, data=data),
                                metadata=metadata, timeout=30)
        return {"upload_id": resp.upload_id, "status": resp.status,
                "final_hash": resp.final_hash, "sha256": digest}
    result = _call(go)
    result["client_upload_id"] = client_id
    result["robot_id_claimed"] = robot_id
    return result


def op_refuse_after_action(stub, metadata, args) -> dict:
    warm = op_upload(stub, metadata, args.robot_id)
    if not warm["ok"]:
        return {"ok": False, "stage": "warm_up", "warm_up": warm}
    admin_token = Path(args.admin_token_file).read_text(encoding="ascii").strip()
    request = urllib.request.Request(
        args.action_url, data=(args.action_body or "{}").encode(), method="POST",
        headers={"authorization": f"Bearer {admin_token}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            action_status, action_body = reply.status, reply.read().decode()
    except urllib.error.HTTPError as exc:
        action_status, action_body = exc.code, exc.read().decode()
    acted_at = time.monotonic()
    if action_status != 200:
        return {"ok": False, "stage": "admin_action", "warm_up": warm,
                "action_status": action_status, "action_body": action_body}
    attempts = []
    while True:
        attempt = op_upload(stub, metadata, args.robot_id)
        attempt["seconds_after_action"] = round(time.monotonic() - acted_at, 3)
        attempts.append(attempt)
        if not attempt["ok"]:
            break
        if attempt["seconds_after_action"] > args.window + 5:
            break
        time.sleep(0.5)
    last = attempts[-1]
    return {"ok": True, "stage": "done", "warm_up": warm, "action_status": action_status,
            "action_body": json.loads(action_body) if action_body else None,
            "accepted_after_action": sum(1 for a in attempts if a["ok"]),
            "refused": not last["ok"], "refusal_code": last["code"],
            "refusal_details": last["details"],
            "seconds_to_refusal": last["seconds_after_action"], "attempts": attempts}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["list", "status", "upload", "refuse-after-action"])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--token-file")
    source.add_argument("--token-env")
    parser.add_argument("--filter")
    parser.add_argument("--upload-id")
    parser.add_argument("--robot-id")
    parser.add_argument("--action-url")
    parser.add_argument("--action-body")
    parser.add_argument("--admin-token-file")
    parser.add_argument("--window", type=float, default=30.0)
    args = parser.parse_args()

    metadata = (("authorization", f"Bearer {_token(args)}"),)
    channel, stub = _stub()
    try:
        if args.command == "list":
            result = op_list(stub, metadata, args)
        elif args.command == "status":
            result = op_status(stub, metadata, args)
        elif args.command == "upload":
            result = op_upload(stub, metadata, args.robot_id)
        else:
            result = op_refuse_after_action(stub, metadata, args)
    finally:
        channel.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
