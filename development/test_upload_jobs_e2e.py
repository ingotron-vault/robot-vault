"""Real HTTP -> config -> queue -> manifests -> gRPC job-outcome regression."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import grpc
import upload_pb2 as pb
import upload_pb2_grpc as pb_grpc

from robot_storage_manager.core import load_or_create_manifest, manifest_path_for, save_manifest


def complete_mcap(payload):
    """Wrap fixture bytes in MCAP magic and a Footer, so the agent treats the
    file as finished. The agent skips an .mcap without a complete footer."""
    magic = b"\x89MCAP0\r\n"
    return magic + payload + b"\x02" + (20).to_bytes(8, "little") + bytes(20) + magic


class HeldUploadService(pb_grpc.UploadServiceServicer):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.received = []
        self.hold_filename = "held.mcap"

    def HealthCheck(self, request, context):  # noqa: N802
        return pb.HealthCheckResponse(status="healthy", checks={"per_robot_concurrency": "1"})

    def UploadSmall(self, request, context):  # noqa: N802
        digest = hashlib.sha256(request.data).hexdigest()
        self.received.append({
            "filename": request.metadata.filename,
            "robot_id": request.metadata.robot_id,
            "bytes": len(request.data),
            "sha256": digest,
            "expected_sha256": request.metadata.expected_sha256,
        })
        if request.metadata.filename == self.hold_filename:
            self.entered.set()
            if not self.release.wait(30):
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "fixture response not released")
        if request.metadata.filename == "rejected.mcap":
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "fixture refuses this recording")
        return pb.UploadResponse(
            upload_id=request.metadata.upload_id, status="completed",
            bytes_received=len(request.data), final_hash=digest,
            storage_path=f"s3://fixture/{request.metadata.filename}",
        )


def wait_for(operation, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = operation()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"Timed out: {description}")


def main():
    artifact_dir = Path(os.environ.get("UPLOAD_JOBS_ARTIFACT_DIR", "tmp/upload-jobs-e2e")).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "checks": [], "jobs": {}, "received": []}
    service = HeldUploadService()
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    pb_grpc.add_UploadServiceServicer_to_server(service, server)
    grpc_port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    process = None

    def stop_http():
        service.release.set()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                report["passed"] = False
                report["teardown_error"] = "HTTP server required forced termination"
        report["teardown"] = {"http_stopped": process is None or process.poll() is not None}

    Path("/tmp/robot_data").mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="job-e2e-", dir="/tmp/robot_data") as temp, ExitStack() as cleanup:
            root = Path(temp)
            held = root / "held.mcap"
            recording = complete_mcap(b"deterministic HTTP upload job fixture\x00" * 127)
            held.write_bytes(recording)
            config = root / "vault.json"
            config.write_text(json.dumps({
                "robot_id": "job-e2e-robot",
                "server": {"host": "127.0.0.1", "port": grpc_port},
                "tls": {"enabled": False},
                "upload_agent": {"parallel_streams": 1, "max_retries": 0, "min_stable_age_seconds": 0},
            }))
            # Keep the bound socket until uvicorn takes ownership: no port race.
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                http_port = listener.getsockname()[1]
                env = dict(os.environ, VAULT_CONFIG_PATH=str(config))
                with (artifact_dir / "http-server.log").open("w") as log:
                    process = subprocess.Popen([
                        sys.executable, "-c",
                        "import uvicorn; from robot_upload_agent.web import create_app; "
                        f"uvicorn.run(create_app(), fd={listener.fileno()}, log_level='warning')",
                    ], env=env, pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
                    # Stop the application before removing recordings that
                    # background upload threads may still be reading.
                    cleanup.callback(stop_http)
                    base = f"http://127.0.0.1:{http_port}"

                    def request(path, payload=None):
                        data = None if payload is None else json.dumps(payload).encode()
                        req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=5) as response:
                            return json.load(response)

                    def ready():
                        if process.poll() is not None:
                            raise AssertionError("HTTP server exited; see http-server.log")
                        try:
                            return request("/api/upload/jobs") is not None
                        except (OSError, urllib.error.URLError):
                            return False

                    wait_for(ready, "HTTP application startup")

                    def start(files):
                        return request("/api/upload/start", {"root": str(root), "files": [str(f) for f in files]})["job_id"]

                    def finished(job_id):
                        def read():
                            job = request(f"/api/upload/jobs/{job_id}")
                            return job if job["status"] == "completed" else None
                        return wait_for(read, f"job {job_id} completion")

                    first_id = start([held])
                    assert service.entered.wait(20), "First job did not reach the real gRPC service"
                    report["checks"].append("first transfer held at remote response boundary")
                    second = finished(start([held]))
                    report["jobs"]["overlap"] = second
                    assert second["files"][0]["status"] == "failed", f"Overlapping job lacks terminal failure: {second}"
                    error = second["files"][0]["error"]
                    assert error and error["status"] == "in_progress" and error.get("detail"), error
                    assert json.loads(manifest_path_for(held).read_text())["status"] == "uploading"
                    assert len(service.received) == 1, "Overlapping job sent duplicate bytes"
                    report["checks"].append("overlap explained; original manifest and transfer preserved")
                    service.release.set()
                    first = finished(first_id)
                    report["jobs"]["first"] = first
                    assert first["files"][0]["status"] == "completed", first
                    expected = hashlib.sha256(recording).hexdigest()
                    assert service.received[0] == {
                        "filename": "held.mcap", "robot_id": "job-e2e-robot",
                        "bytes": len(recording), "sha256": expected, "expected_sha256": expected,
                    }, service.received
                    report["checks"].append("real config identity and exact bytes/digest reached gRPC")
                    repeated = finished(start([held]))
                    report["jobs"]["already_completed"] = repeated
                    assert repeated["files"][0]["status"] == "completed" and len(service.received) == 1
                    report["checks"].append("completed recording selected again without retransmission")
                    rejected = root / "rejected.mcap"
                    rejected.write_bytes(complete_mcap(b"rejection fixture"))
                    failed = finished(start([rejected]))
                    report["jobs"]["rejected"] = failed
                    assert failed["files"][0]["status"] == "failed" and failed["files"][0]["error"], failed
                    report["checks"].append("remote rejection retains terminal explanation")

                    # Break a later sidecar only after all candidates have
                    # been prepared and the first transfer is held remotely.
                    # This raises from the actual filesystem, not an injected
                    # queue, after the first completion is durable.
                    partial = [root / name for name in ("partial-first.mcap", "partial-second.mcap", "partial-third.mcap")]
                    for index, path in enumerate(partial):
                        path.write_bytes(complete_mcap(b"partial fixture" * (3 - index)))
                    manifest = load_or_create_manifest(partial[0])
                    manifest["priority"] = "critical"
                    save_manifest(manifest)
                    service.hold_filename = partial[0].name
                    service.entered.clear()
                    service.release.clear()
                    partial_id = start(partial)
                    assert service.entered.wait(20), "Partial batch did not reach held first transfer"
                    for path in partial[1:]:
                        sidecar = manifest_path_for(path)
                        sidecar.unlink()
                        sidecar.mkdir()
                    service.release.set()
                    partial_job = finished(partial_id)
                    report["jobs"]["partial_queue_exception"] = partial_job
                    outcomes = {item["path"]: item for item in partial_job["files"]}
                    assert outcomes[str(partial[0])]["status"] == "completed", partial_job
                    assert all(outcomes[str(path)]["status"] == "failed" and outcomes[str(path)]["error"] for path in partial[1:]), partial_job
                    assert not any(entry["filename"] in {path.name for path in partial[1:]} for entry in service.received)
                    report["checks"].append("durable partial success survives later filesystem exception; unattempted files explained")

                    replacement = [root / name for name in ("replace-first.mcap", "replace-blocker.mcap")]
                    for path in replacement:
                        path.write_bytes(complete_mcap(b"original recording"))
                    manifest = load_or_create_manifest(replacement[0])
                    manifest["priority"] = "critical"
                    save_manifest(manifest)
                    service.hold_filename = replacement[1].name
                    service.entered.clear()
                    service.release.clear()
                    replacement_id = start(replacement)
                    assert service.entered.wait(20), "Replacement batch did not reach held later transfer"
                    assert json.loads(manifest_path_for(replacement[0]).read_text())["status"] == "completed"
                    new_recording = root / "replacement.tmp"
                    new_recording.write_bytes(complete_mcap(b"replacement bytes were never uploaded"))
                    new_recording.replace(replacement[0])
                    blocker_sidecar = manifest_path_for(replacement[1])
                    blocker_sidecar.unlink()
                    blocker_sidecar.mkdir()
                    service.release.set()
                    replacement_job = finished(replacement_id)
                    report["jobs"]["replacement_after_completion"] = replacement_job
                    outcomes = {item["path"]: item for item in replacement_job["files"]}
                    assert outcomes[str(replacement[0])]["status"] == "failed", replacement_job
                    assert outcomes[str(replacement[0])]["error"]["status"] == "recording_changed", replacement_job
                    report["checks"].append("stale completed sidecar does not claim replacement bytes completed")
                    assert all(f["status"] in ("completed", "failed") for j in report["jobs"].values() for f in j["files"])
                    report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        stop_http()
        server.stop(0).wait()
        report["received"] = service.received
        (artifact_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
