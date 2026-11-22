import threading
from concurrent import futures
from pathlib import Path

import grpc

import upload_pb2 as pb
import upload_pb2_grpc as pb_grpc

from robot_upload_agent.transport import GrpcUploadTransport


class DummyUploadService(pb_grpc.UploadServiceServicer):
	def UploadSmall(self, request, context):  # noqa: N802
		resp = pb.UploadResponse(
			upload_id=request.metadata.upload_id or "test-id",
			status="completed",
			bytes_received=len(request.data),
			final_hash=request.metadata.expected_sha256 or "",
			storage_path=f"s3://bucket/{request.metadata.filename}",
		)
		return resp

	def Upload(self, request_iterator, context):  # noqa: N802
		first = next(request_iterator)
		bytes_received = 0
		for chunk in request_iterator:
			bytes_received += len(chunk.chunk.data)
		return pb.UploadResponse(upload_id=first.metadata.upload_id or "stream-id", status="completed", bytes_received=bytes_received)


class MetadataCapturingService(DummyUploadService):
	"""DummyUploadService that records the call metadata it was handed.

	Used to assert the shared ingest token actually reaches the wire: the real
	ingest server rejects an RPC without `authorization` when
	INGEST_AUTH_TOKEN is configured, and a client-side regression there would
	otherwise only surface as a fleet-wide UNAUTHENTICATED outage.
	"""

	def __init__(self):
		self.seen = []

	def UploadSmall(self, request, context):  # noqa: N802
		self.seen.append(dict(context.invocation_metadata()))
		return super().UploadSmall(request, context)


def _start_server(servicer=None):
	# Port 0: the kernel assigns a free port.  A fixed port in the ephemeral
	# range (50070 was inside ip_local_port_range on at least one test host)
	# can be squatted by any concurrent socket, failing the bind.
	server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
	pb_grpc.add_UploadServiceServicer_to_server(servicer or DummyUploadService(), server)
	port = server.add_insecure_port("[::]:0")
	server.start()
	return server, port


def test_auth_token_is_sent_as_bearer_metadata(tmp_path: Path, monkeypatch):
	"""The token reaches the server as `authorization: Bearer <token>`,
	from the config value or INGEST_AUTH_TOKEN, and nothing is sent when
	neither is set."""
	svc = MetadataCapturingService()
	server, port = _start_server(svc)
	mf = {
		"path": str(tmp_path / "a.mcap"),
		"file_size_bytes": 3,
		"upload_id": "u-1",
		"sha256_hash": "",
	}
	try:
		# 1) An explicit token passed in code wins over the env.
		monkeypatch.setenv("INGEST_AUTH_TOKEN", "env-token")
		GrpcUploadTransport("127.0.0.1", port, use_tls=False, auth_token="cfg-token", robot_id="rover-7").upload_small(mf, b"abc")
		assert svc.seen[-1].get("authorization") == "Bearer cfg-token"

		# 2) No explicit token -> the environment variable compose sets.
		GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7").upload_small(mf, b"abc")
		assert svc.seen[-1].get("authorization") == "Bearer env-token"

		# 3) Neither -> no credential at all (a server with no token configured
		# still works, as do the fake servers in these tests).
		monkeypatch.delenv("INGEST_AUTH_TOKEN", raising=False)
		GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7").upload_small(mf, b"abc")
		assert "authorization" not in svc.seen[-1]
	finally:
		server.stop(0)
