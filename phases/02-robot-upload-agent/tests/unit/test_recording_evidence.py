"""Recording status uses validated receipts, never local completion alone."""
import pytest
from fastapi.testclient import TestClient

from robot_storage_manager.core import manifest_path_for, read_manifest
from robot_upload_agent.core import UploadAgentConfig, UploadQueue
from robot_upload_agent import web


@pytest.fixture
def recording(tmp_path, monkeypatch):
    path = tmp_path / "capture.mcap"
    path.write_bytes(b"capture bytes")
    queue = UploadQueue(tmp_path, UploadAgentConfig(min_stable_age_seconds=0))
    manifest = queue._prepare(path)
    monkeypatch.setattr(web, "BASE_ROOT", tmp_path)
    client = TestClient(web.create_app(queue_factory=lambda root: queue))
    return path, queue, manifest, client


def _complete(recording, monkeypatch, **overrides):
    path, queue, manifest, _ = recording
    response = {"status": "completed", "upload_id": manifest["upload_id"],
                "bytes_received": path.stat().st_size, "final_hash": manifest["sha256_hash"],
                "storage_path": "s3://vault/robot/capture.mcap"}
    response.update(overrides)
    monkeypatch.setattr(queue, "_upload_file_with_retry", lambda *args, **kwargs: response)
    queue._upload_one(path, manifest)


def _history(recording):
    path, _, _, client = recording
    response = client.get("/api/upload/history", params={"root": str(path.parent)})
    assert response.status_code == 200
    return response.json()["files"][0]


def test_completed_upload_receipt_survives_restart_and_never_approves_deletion(recording, monkeypatch):
    _complete(recording, monkeypatch)
    path, queue, _, client = recording
    row = _history(recording)
    assert row["server_verified"] is True
    assert row["server_verified_at"]
    assert row["server_storage_path"] == "s3://vault/robot/capture.mcap"
    assert row["safe_to_remove_local"] is False
    assert queue.list_files()[0]["server_verified"] is True
    assert client.get("/api/upload/jobs").json() == []


@pytest.mark.parametrize("overrides", [
    {"final_hash": "0" * 64}, {"final_hash": ""}, {"upload_id": "another-upload"},
    {"bytes_received": 0}, {"storage_path": ""},
])
def test_completion_without_matching_receipt_is_unknown(recording, monkeypatch, overrides):
    _complete(recording, monkeypatch, **overrides)
    row = _history(recording)
    assert row["status"] == "completed"
    assert row["server_verified"] is None
    assert row["safe_to_remove_local"] is False


@pytest.mark.parametrize("change", ["rewrite", "remove"])
def test_receipt_does_not_verify_missing_or_changed_local_file(recording, monkeypatch, change):
    _complete(recording, monkeypatch)
    path = recording[0]
    if change == "rewrite":
        path.write_bytes(b"changed bytes")
    else:
        path.unlink()
    row = _history(recording)
    assert row["server_verified"] is None
    assert row["data_file_present"] is (change != "remove")
    assert row["safe_to_remove_local"] is False


def test_changed_during_upload_gets_no_receipt(recording, monkeypatch):
    path, queue, manifest, _ = recording
    def upload(*args, **kwargs):
        path.write_bytes(b"new recording")
        return {"status": "completed", "upload_id": manifest["upload_id"],
                "bytes_received": manifest["file_size_bytes"], "final_hash": manifest["sha256_hash"],
                "storage_path": "s3://vault/capture.mcap"}
    monkeypatch.setattr(queue, "_upload_file_with_retry", upload)
    queue._upload_one(path, manifest)
    assert _history(recording)["server_verified"] is None


def test_failure_reason_persists_and_clears_on_retry(recording, monkeypatch):
    path, queue, manifest, _ = recording
    def fail(*args, **kwargs):
        raise ConnectionError("connection dropped")
    monkeypatch.setattr(queue, "_upload_file_with_retry", fail)
    queue._upload_one(path, manifest)
    row = _history(recording)
    assert row["status"] == "failed"
    assert row["last_upload_error"]["detail"] == "connection dropped"
    assert row["upload_updated_at"]
    _complete(recording, monkeypatch)
    assert _history(recording)["last_upload_error"] is None


def test_file_listing_does_not_create_or_reconcile_manifests(recording):
    path, _, _, client = recording
    sidecar = manifest_path_for(path)
    sidecar.unlink(missing_ok=True)
    response = client.get("/api/files", params={"root": str(path.parent)})
    assert response.status_code == 200
    assert response.json()[0]["status"] == "pending"
    assert response.json()[0]["server_verified"] is None
    assert read_manifest(sidecar) is None


def test_grpc_failure_preserves_code_for_recovery_guidance(recording, monkeypatch):
    import grpc
    path, queue, manifest, _ = recording
    class Failure(Exception):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE
    def fail(*args, **kwargs):
        raise Failure("connection dropped")
    monkeypatch.setattr(queue, "_upload_file_with_retry", fail)
    queue._upload_one(path, manifest)
    assert _history(recording)["last_upload_error"]["code"] == "UNAVAILABLE"


def test_rewritten_completed_file_is_pending_in_listing_but_history_is_preserved(recording, monkeypatch):
    _complete(recording, monkeypatch)
    path, _, _, client = recording
    path.write_bytes(b"replacement recording with different content")
    sidecar = manifest_path_for(path)
    previous_manifest = sidecar.read_bytes()
    response = client.get("/api/files", params={"root": str(path.parent)})
    assert response.status_code == 200
    row = response.json()[0]
    assert row["status"] == "pending"
    assert row["server_verified"] is None
    assert row["size"] == path.stat().st_size
    assert sidecar.read_bytes() == previous_manifest
    historical = _history(recording)
    assert historical["status"] == "completed"
    assert historical["server_verified"] is None
