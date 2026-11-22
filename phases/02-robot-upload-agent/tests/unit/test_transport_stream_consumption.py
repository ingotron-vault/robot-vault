"""`upload_stream` must consume the iterable it is handed.

If `transport.upload_stream(manifest, chunks)` ignored its `chunks` argument and
re-opened the file itself, two documented features would be inert for every file
above `small_file_threshold_mb`, and every byte would be read twice:

* `upload_agent.bandwidth_mbps` -- the pacing sleeps live in the caller's
  generator, so nothing would be paced, and there would be no way to simulate a
  slow link from inside the agent.
* `upload_agent.compute_chunk_hashes` -- the per-chunk digests would never reach
  the wire, so the server's chunk verification would never run.
"""
import hashlib
from pathlib import Path

import grpc
import pytest
import upload_pb2 as pb
import upload_pb2_grpc as pb_grpc
from concurrent import futures

from robot_vault_config.model import (
	DEFAULT_CHUNK_SIZE_BYTES,
	MAX_CHUNK_SIZE_BYTES,
	MIN_S3_PART_BYTES,
	validate_chunk_size_bytes,
)
from robot_upload_agent.transport import (
	DEFAULT_CHUNK_SIZE_BYTES as TRANSPORT_DEFAULT_CHUNK_SIZE,
	GrpcUploadTransport,
	number_chunks,
	resolve_chunk_size,
)


class RecordingUploadService(pb_grpc.UploadServiceServicer):
	"""Records exactly what arrived on the wire."""

	def __init__(self):
		self.chunks = []
		self.metadata = None

	def Upload(self, request_iterator, context):  # noqa: N802
		first = next(request_iterator)
		self.metadata = first.metadata
		total = 0
		for req in request_iterator:
			c = req.chunk
			self.chunks.append({
				"chunk_number": c.chunk_number,
				"offset": c.offset,
				"len": len(c.data),
				"is_final": c.is_final,
				"chunk_hash": c.chunk_hash,
				"data": c.data,
			})
			total += len(c.data)
		return pb.UploadResponse(upload_id=first.metadata.upload_id, status="completed", bytes_received=total)

	def ResumeUpload(self, request, context):  # noqa: N802
		# A new resume-version-1 session: nothing is saved yet.
		return pb.ResumeUploadResponse(upload_id=request.upload_id, bytes_received=0, status="unknown", resume_version=1)

	def HealthCheck(self, request, context):  # noqa: N802
		return pb.HealthCheckResponse(status="healthy", checks={"per_robot_concurrency": "3"})


def _serve(servicer):
	"""Start a throwaway gRPC server on a kernel-assigned port.

	A fixed port in 50081-50086 would sit INSIDE the Linux ephemeral range
	(32768-60999): a busy machine could already be holding it as an outbound
	source port, and the bind would fail with "Address already in use". Binding
	port 0 and reading back what the kernel gave us avoids the collision, and
	removes the "which test owns which port" bookkeeping too.
	"""
	server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
	pb_grpc.add_UploadServiceServicer_to_server(servicer, server)
	port = server.add_insecure_port("[::]:0")
	assert port != 0, "the kernel refused to assign a port to the test server"
	server.start()
	return server, port


def _manifest(path: Path, chunk_size: int, **extra) -> dict:
	mf = {
		"path": str(path),
		"file_size_bytes": path.stat().st_size,
		"upload_id": "u-stream-1",
		"sha256_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
		"chunk_size_bytes": chunk_size,
	}
	mf.update(extra)
	return mf


def test_the_caller_supplied_chunks_are_what_reaches_the_wire(tmp_path: Path):
	"""If the transport rebuilt its own generator, the sentinel bytes below would
	never appear on the wire -- which is exactly how `bandwidth_mbps` stayed
	inert while looking implemented."""
	svc = RecordingUploadService()
	server, port = _serve(svc)
	try:
		f = tmp_path / "a.mcap"
		f.write_bytes(b"A" * 6)
		mf = _manifest(f, chunk_size=4)
		transport = GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7")

		consumed = []

		def chunks():
			for buf in (b"XXXX", b"YY"):
				consumed.append(buf)
				yield buf

		resp = transport.upload_stream(mf, chunks())
		assert resp["status"] == "completed"
		assert consumed == [b"XXXX", b"YY"], "the caller's generator must be driven"
		assert [c["data"] for c in svc.chunks] == [b"XXXX", b"YY"]
		assert resp["bytes_received"] == 6
	finally:
		server.stop(0)


def test_numbering_offsets_and_is_final_come_from_arrival_order(tmp_path: Path):
	"""The server accepts chunk_number == expected_next and nothing else,
	and needs is_final on the last chunk to complete the multipart upload."""
	svc = RecordingUploadService()
	server, port = _serve(svc)
	try:
		f = tmp_path / "b.mcap"
		f.write_bytes(b"b" * 10)
		mf = _manifest(f, chunk_size=4)
		transport = GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7")
		transport.upload_stream(mf, iter([b"0" * 4, b"1" * 4, b"2" * 2]))

		assert [c["chunk_number"] for c in svc.chunks] == [0, 1, 2]
		assert [c["offset"] for c in svc.chunks] == [0, 4, 8]
		assert [c["is_final"] for c in svc.chunks] == [False, False, True]
	finally:
		server.stop(0)


def test_compute_chunk_hashes_reaches_the_wire(tmp_path: Path):
	"""The server verifies `chunk_hash` when it is non-empty (main.rs
	`chunk_hash_mismatch`), so the client must send it."""
	svc = RecordingUploadService()
	server, port = _serve(svc)
	try:
		f = tmp_path / "c.mcap"
		f.write_bytes(b"c" * 6)
		mf = _manifest(f, chunk_size=4, compute_chunk_hashes=True)
		transport = GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7")
		transport.upload_stream(mf, iter([b"c" * 4, b"c" * 2]))

		assert [c["chunk_hash"] for c in svc.chunks] == [
			hashlib.sha256(b"c" * 4).hexdigest(),
			hashlib.sha256(b"c" * 2).hexdigest(),
		]
	finally:
		server.stop(0)


def test_chunk_hashes_are_omitted_by_default(tmp_path: Path):
	svc = RecordingUploadService()
	server, port = _serve(svc)
	try:
		f = tmp_path / "d.mcap"
		f.write_bytes(b"d" * 6)
		mf = _manifest(f, chunk_size=4)
		GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7").upload_stream(mf, iter([b"d" * 4, b"d" * 2]))
		assert [c["chunk_hash"] for c in svc.chunks] == ["", ""]
	finally:
		server.stop(0)


def test_no_chunks_argument_falls_back_to_the_full_plan(tmp_path: Path):
	"""The plan remains the single definition of what a full (re)send looks like."""
	svc = RecordingUploadService()
	server, port = _serve(svc)
	try:
		f = tmp_path / "e.mcap"
		f.write_bytes(bytes(range(10)))
		mf = _manifest(f, chunk_size=4)
		GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7").upload_stream(mf)
		assert b"".join(c["data"] for c in svc.chunks) == bytes(range(10))
		assert [c["chunk_number"] for c in svc.chunks] == [0, 1, 2]
	finally:
		server.stop(0)


def test_healthcheck_advertised_per_robot_limit_is_read(tmp_path: Path):
	svc = RecordingUploadService()
	server, port = _serve(svc)
	try:
		transport = GrpcUploadTransport("127.0.0.1", port, use_tls=False, robot_id="rover-7")
		assert transport.health_check()["status"] == "healthy"
		assert transport.server_per_robot_stream_limit() == 3
	finally:
		server.stop(0)


# --- the numbering helper, unit-level ---------------------------------------

def test_number_chunks_rejects_chunk_size_drift():
	"""Part boundaries must never move between attempts. A
	drifted non-final buffer shifts every subsequent S3 part -- and one under
	5 MiB is rejected by the server after the bytes have already been sent."""
	with pytest.raises(ValueError, match="chunk_size drift"):
		list(number_chunks(iter([b"x" * 4, b"x" * 3, b"x" * 4]), 4))
	# The FINAL chunk is allowed to be short; that is the whole point of is_final.
	assert [n for n, _o, _d, _f in number_chunks(iter([b"x" * 4, b"x" * 1]), 4)] == [0, 1]


def test_number_chunks_skips_empty_buffers():
	out = list(number_chunks(iter([b"", b"ab", b"", b"c"]), 2))
	assert [(n, o, d, f) for n, o, d, f in out] == [(0, 0, b"ab", False), (1, 2, b"c", True)]


def test_number_chunks_on_an_empty_source_yields_nothing():
	assert list(number_chunks(iter([]), 4)) == []


# --- the chunk-size ceiling ------------------------------------------------

def test_default_chunk_size_is_5_mib_and_inside_the_bounds():
	"""5 MiB, not 16.

	Against the pipelined server the measured throughput falls as the chunk
	grows: 5 MiB 193.6 > 8 MiB 166.1 > 16 MiB 150.2 MiB/s. (A server that awaits
	each upload_part inline favours larger chunks, so the figure depends on
	the server.) This pins the default to the measured winner so a future edit
	has to re-measure rather than restore a stale figure.
	"""
	assert DEFAULT_CHUNK_SIZE_BYTES == 5 * 1024 * 1024
	assert MIN_S3_PART_BYTES <= DEFAULT_CHUNK_SIZE_BYTES <= MAX_CHUNK_SIZE_BYTES


@pytest.mark.parametrize(
	"bad",
	[0, -1, "nonsense", 1024 * 1024, MIN_S3_PART_BYTES - 1, MAX_CHUNK_SIZE_BYTES + 1, 48 * 1024 * 1024],
)
def test_chunk_size_outside_the_s3_floor_and_stream_cap_is_refused(bad):
	"""Below 5 MiB the server rejects every non-final chunk
	(`chunk_too_small_for_s3_part`); above 8 MiB it rejects streaming chunks.
	Either is a config error, not something to repair silently."""
	with pytest.raises(ValueError, match="chunk_size_bytes"):
		validate_chunk_size_bytes(bad)


def test_chunk_size_bounds_are_accepted():
	assert validate_chunk_size_bytes(MIN_S3_PART_BYTES) == MIN_S3_PART_BYTES
	assert validate_chunk_size_bytes(MAX_CHUNK_SIZE_BYTES) == MAX_CHUNK_SIZE_BYTES


def test_transport_fallback_chunk_size_respects_the_s3_floor():
	"""The fallback must not be 1 MiB, which is below the floor: an upload that
	fell back to it would fail every non-final chunk."""
	assert TRANSPORT_DEFAULT_CHUNK_SIZE == DEFAULT_CHUNK_SIZE_BYTES
	assert resolve_chunk_size({}) >= MIN_S3_PART_BYTES
