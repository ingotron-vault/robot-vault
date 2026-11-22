"""Real gRPC client tests for negotiated durable multipart resume."""
import hashlib
import os
from concurrent import futures
from pathlib import Path

import grpc
import pytest
import upload_pb2 as pb
import upload_pb2_grpc as pb_grpc

from robot_storage_manager.core import load_or_create_manifest, save_manifest
from robot_upload_agent.core import UploadAgentConfig, UploadQueue, _FileChunkSource, _pin_upload_chunk_size
from robot_upload_agent.bandwidth import BandwidthLimiter
from robot_upload_agent.transport import GrpcUploadTransport, compute_chunks_plan
from robot_upload_agent.retry import call_with_retry


class ResumeService(pb_grpc.UploadServiceServicer):
    def __init__(self, response, failure=None):
        self.response = response
        self.failure = failure
        self.requests = []
        self.probes = []

    def ResumeUpload(self, request, context):
        self.probes.append(request)
        if self.failure:
            context.abort(self.failure, "resume probe failed")
        return self.response

    def Upload(self, requests, context):
        self.requests.extend(requests)
        metadata = self.requests[0].metadata
        return pb.UploadResponse(upload_id=metadata.upload_id, status="completed", bytes_received=metadata.file_size,
                                 final_hash=metadata.expected_sha256, storage_path="recordings/resumed.mcap")


@pytest.fixture
def serve():
    servers = []

    def start(service):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        pb_grpc.add_UploadServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        servers.append(server)
        return GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7")

    yield start
    for server in servers:
        server.stop(0).wait()


def manifest_for(tmp_path, chunk_size=4):
    path = tmp_path / "data.mcap"
    path.write_bytes(b"0123456789")
    manifest = load_or_create_manifest(path)
    manifest.update(upload_id="resume-test", sha256_hash=hashlib.sha256(path.read_bytes()).hexdigest(), chunk_size_bytes=chunk_size)
    return path, manifest


def resume_response(**changes):
    fields = dict(upload_id="resume-test", status="uploading", bytes_received=6, missing_chunks=[1],
                  resume_version=1, chunk_size_bytes=4, resume_session_id="session-a")
    fields.update(changes)
    return pb.ResumeUploadResponse(**fields)


def test_only_missing_part_is_sent_with_its_original_number_and_offset(tmp_path, serve):
    path, manifest = manifest_for(tmp_path)
    service = ResumeService(resume_response())
    transport = serve(service)
    progress = []
    charged = []
    class Limiter:
        def acquire(self, count):
            charged.append(count)
    chunks = _FileChunkSource(path, manifest, 4, Limiter(), lambda *values: progress.append(values))
    transport.upload_stream(manifest, chunks)

    assert service.probes[0].resume_version == 1
    metadata = service.requests[0].metadata
    assert (metadata.resume_version, metadata.chunk_size_bytes, metadata.resume_session_id) == (1, 4, "session-a")
    sent = [request.chunk for request in service.requests[1:]]
    assert [(chunk.chunk_number, chunk.offset, chunk.data, chunk.is_final) for chunk in sent] == [(1, 4, b"4567", True)]
    assert progress[-1] == (str(path), 4, 4), "attempt progress must not count already-saved bytes as sent"
    assert charged == [4], "saved resume parts must not consume the bandwidth allowance"
    saved = load_or_create_manifest(path)
    assert saved["saved_bytes"] == 6
    assert saved["resume_supported"] is True


@pytest.mark.parametrize("response", [
    resume_response(bytes_received=10, missing_chunks=[]),
    resume_response(bytes_received=10, missing_chunks=[], status="completed", resume_session_id="", chunk_size_bytes=0),
])
def test_all_parts_saved_or_completed_upload_sends_metadata_only(tmp_path, serve, response):
    _path, manifest = manifest_for(tmp_path)
    service = ResumeService(response)
    result = serve(service).upload_stream(manifest)
    assert result["status"] == "completed"
    assert len(service.requests) == 1
    assert service.requests[0].HasField("metadata")


def test_new_server_with_no_session_starts_at_zero(tmp_path, serve):
    _path, manifest = manifest_for(tmp_path)
    service = ResumeService(resume_response(bytes_received=0, missing_chunks=[], chunk_size_bytes=0, resume_session_id="", status="unknown"))
    serve(service).upload_stream(manifest)
    assert b"".join(request.chunk.data for request in service.requests[1:]) == b"0123456789"


@pytest.mark.parametrize("changes", [
    {"upload_id": "wrong-recording"}, {"bytes_received": 5}, {"missing_chunks": [1, 1]},
    {"missing_chunks": [-1]}, {"missing_chunks": [3]}, {"chunk_size_bytes": 5},
    {"resume_session_id": ""}, {"status": "completed", "bytes_received": 9},
    {"resume_version": 0}, {"resume_version": 2},
])
def test_inconsistent_resume_proof_is_rejected_before_sending(tmp_path, serve, changes):
    _path, manifest = manifest_for(tmp_path)
    service = ResumeService(resume_response(**changes))
    with pytest.raises(ValueError):
        serve(service).upload_stream(manifest)
    assert service.requests == []


@pytest.mark.parametrize("code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.DEADLINE_EXCEEDED])
def test_probe_failure_does_not_silently_restart_the_upload(tmp_path, serve, code):
    _path, manifest = manifest_for(tmp_path)
    service = ResumeService(None, code)
    with pytest.raises(grpc.RpcError) as caught:
        serve(service).upload_stream(manifest)
    assert caught.value.code() == code
    assert service.requests == []


def test_changed_file_cannot_supply_parts_for_its_previous_digest(tmp_path):
    path, manifest = manifest_for(tmp_path)
    chunks = _FileChunkSource(path, manifest, 4, BandwidthLimiter(0))
    path.write_bytes(b"abcdefghij")
    os.utime(path, ns=(manifest["mtime_ns"] + 1_000_000_000, manifest["mtime_ns"] + 1_000_000_000))
    with pytest.raises(ValueError, match="changed after hashing"):
        list(chunks.for_plan([(1, 4, 4)]))


def test_part_pacing_only_applies_to_transmitted_bytes(tmp_path):
    path, manifest = manifest_for(tmp_path)
    charged = []
    class Limiter:
        def acquire(self, count):
            charged.append(count)
    chunks = _FileChunkSource(path, manifest, 4, Limiter())
    assert list(chunks.for_plan([(1, 4, 4)])) == [b"4567"]
    assert charged == [4]


def test_chunk_size_survives_a_client_restart_and_configuration_change(tmp_path):
    path, manifest = manifest_for(tmp_path, 5 * 1024 * 1024)
    save_manifest(manifest)
    queue = UploadQueue(tmp_path, UploadAgentConfig(chunk_size_bytes=8 * 1024 * 1024, min_stable_age_seconds=0))
    prepared = queue._prepare(path)
    assert prepared["chunk_size_bytes"] == 5 * 1024 * 1024
    assert prepared["upload_id"] == "resume-test"


def test_new_file_gets_a_large_enough_part_size_and_clears_old_progress():
    manifest = {"file_size_bytes": 60 * 1024**3, "upload_id": None, "saved_bytes": 500, "resume_supported": True}
    _pin_upload_chunk_size(manifest, 5 * 1024 * 1024)
    assert 5 * 1024 * 1024 <= manifest["chunk_size_bytes"] <= 8 * 1024 * 1024
    assert len(compute_chunks_plan(manifest["file_size_bytes"], manifest["chunk_size_bytes"])) <= 10000
    assert "saved_bytes" not in manifest
    assert "resume_supported" not in manifest


def test_files_larger_than_the_multipart_limit_fail_clearly():
    with pytest.raises(ValueError, match="10000 upload parts"):
        _pin_upload_chunk_size({"file_size_bytes": 81 * 1024**3}, 5 * 1024 * 1024)


def test_stale_session_retries_with_a_new_probe_and_original_part_offsets(tmp_path, serve):
    class ReprobeService(ResumeService):
        def ResumeUpload(self, request, context):
            self.probes.append(request)
            return resume_response() if len(self.probes) == 1 else resume_response(
                bytes_received=8, missing_chunks=[2], resume_session_id="session-b")

        def Upload(self, requests, context):
            if len(self.probes) == 1:
                assert next(requests).metadata.resume_session_id == "session-a"
                context.abort(grpc.StatusCode.ABORTED, "resume session changed")
            return super().Upload(requests, context)

    _path, manifest = manifest_for(tmp_path)
    service = ReprobeService(None)
    transport = serve(service)
    result, outcome = call_with_retry(lambda _: transport.upload_stream(manifest), max_retries=1, base_backoff_seconds=0)
    assert result["status"] == "completed"
    assert outcome.attempts == 2
    assert len(service.probes) == 2
    assert service.requests[0].metadata.resume_session_id == "session-b"
    assert [(request.chunk.chunk_number, request.chunk.offset, request.chunk.data) for request in service.requests[1:]] == [(2, 8, b"89")]
