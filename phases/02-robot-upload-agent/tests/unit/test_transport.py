from pathlib import Path
from types import SimpleNamespace

import pytest

from robot_upload_agent.core import UploadAgentConfig, UploadQueue


_MCAP_MAGIC = b"\x89MCAP0\r\n"
_MCAP_END = b"\x02" + (20).to_bytes(8, "little") + bytes(20) + _MCAP_MAGIC


def complete_mcap(payload: bytes) -> bytes:
	"""A complete MCAP envelope; the upload gate waits for the footer."""
	return _MCAP_MAGIC + payload + _MCAP_END


class FakeTransport:
	def __init__(self):
		self.calls = []

	def upload_small(self, manifest, data, compress=False):
		self.calls.append(("small", len(data), manifest.get("upload_id")))
		return {"status": "completed"}

	def upload_stream(self, manifest, chunks, compress=False):
		n = 0
		for c in chunks:
			n += len(c)
		self.calls.append(("stream", n, manifest.get("upload_id")))
		return {"status": "completed"}


def test_resume_yields_full_plan():
	"""Whenever resuming, compute_chunks_plan must re-send every chunk."""
	from robot_upload_agent.transport import compute_chunks_plan
	file_size = 10 * 1024 * 1024
	chunk_size = 2 * 1024 * 1024
	full = [0, 1, 2, 3, 4]
	# Server reports prior progress (bytes_received > 0): still the full plan.
	p_progress = compute_chunks_plan(file_size, chunk_size, resume_bytes=6 * 1024 * 1024)
	assert [p[0] for p in p_progress] == full
	# Server reports bytes_received=0 ("start over"): full plan.
	p_zero = compute_chunks_plan(file_size, chunk_size, resume_bytes=0)
	assert [p[0] for p in p_zero] == full
	# Server reports a missing_chunks subset: ignored, full plan.
	p_missing = compute_chunks_plan(file_size, chunk_size, missing_chunks=[2, 4])
	assert [p[0] for p in p_missing] == full
	# The plan covers the whole file contiguously (offset 0, no gaps).
	assert p_progress[0][1] == 0
	assert sum(length for _, _, length in p_progress) == file_size
	for i, (_, offset, _) in enumerate(p_progress):
		assert offset == i * chunk_size


def test_chunk_size_pinned_to_persisted_value():
	"""chunk_size is taken from the persisted manifest value, pinned across retries."""
	from robot_upload_agent.transport import resolve_chunk_size, DEFAULT_CHUNK_SIZE_BYTES
	# The persisted value is honored exactly (part boundaries stay stable).
	assert resolve_chunk_size({"chunk_size_bytes": 5 * 1024 * 1024}) == 5 * 1024 * 1024
	# A string-typed persisted value is coerced.
	assert resolve_chunk_size({"chunk_size_bytes": "8388608"}) == 8388608
	# Missing or invalid -> deterministic default (still identical every retry).
	assert resolve_chunk_size({}) == DEFAULT_CHUNK_SIZE_BYTES
	assert resolve_chunk_size({"chunk_size_bytes": 0}) == DEFAULT_CHUNK_SIZE_BYTES
	assert resolve_chunk_size({"chunk_size_bytes": "bogus"}) == DEFAULT_CHUNK_SIZE_BYTES


def test_auth_token_resolution(monkeypatch):
	"""Explicit token > INGEST_AUTH_TOKEN > no credential."""
	from robot_upload_agent.transport import _auth_metadata, resolve_auth_token

	monkeypatch.delenv("INGEST_AUTH_TOKEN", raising=False)
	assert resolve_auth_token(None) is None
	assert resolve_auth_token("  ") is None
	assert resolve_auth_token("cfg") == "cfg"

	monkeypatch.setenv("INGEST_AUTH_TOKEN", "from-env")
	assert resolve_auth_token(None) == "from-env"
	assert resolve_auth_token("cfg") == "cfg"
	# Whitespace-only env var is treated as unset, not as a token of spaces.
	monkeypatch.setenv("INGEST_AUTH_TOKEN", "   ")
	assert resolve_auth_token(None) is None

	# Metadata shape must match what the ingest server parses.
	assert _auth_metadata(None) == ()
	assert _auth_metadata("tok") == (("authorization", "Bearer tok"),)


def test_transport_selection(tmp_path: Path, monkeypatch):
	root = tmp_path / "robot" / "2025-08-14"
	root.mkdir(parents=True)
	small = root / "s.mcap"
	big = root / "b.mcap"
	small.write_bytes(complete_mcap(b"x" * (1024 * 1024)))  # 1MB
	big.write_bytes(complete_mcap(b"x" * (3 * 1024 * 1024)))  # 3MB

	from robot_storage_manager.core import load_or_create_manifest, save_manifest
	for p in (small, big):
		mf = load_or_create_manifest(p)
		save_manifest(mf)

	# min_stable_age_seconds=0: the files are written and uploaded in the same
	# instant, which the mtime stability gate would otherwise defer.
	cfg = UploadAgentConfig(small_file_threshold_mb=2, chunk_size_bytes=5 * 1024 * 1024, compute_chunk_hashes=True, min_stable_age_seconds=0)
	q = UploadQueue(root=tmp_path / "robot", config=cfg)
	# Inject fake transport to avoid importing grpc
	q.transport = FakeTransport()

	q.run_once()
	q.run_once()

	kinds = [c[0] for c in q.transport.calls]
	assert "small" in kinds and "stream" in kinds


class _FakeChannel:
	def close(self):
		pass


class _FakeGrpc:
	def __init__(self):
		self.credential_calls = []
		self.secure_calls = []
		self.insecure_calls = []

	def ssl_channel_credentials(self, **kwargs):
		self.credential_calls.append(kwargs)
		return "credentials"

	def secure_channel(self, address, credentials, options):
		self.secure_calls.append((address, credentials, options))
		return _FakeChannel()

	def insecure_channel(self, address, options):
		self.insecure_calls.append((address, options))
		return _FakeChannel()


def _patch_grpc(monkeypatch):
	import robot_upload_agent.transport as transport_module

	fake = _FakeGrpc()
	monkeypatch.setattr(transport_module, "grpc", fake)
	# Constructor behavior is the subject of these tests, not generated stubs.
	monkeypatch.setattr(transport_module, "pb_grpc", None)
	return transport_module, fake


@pytest.mark.parametrize(
	"kwargs",
	[
		{"root_certificates": b"ca"},
		{"client_cert_chain": b"cert", "client_private_key": b"key"},
	],
)
def test_plaintext_transport_rejects_tls_material(monkeypatch, kwargs):
	transport_module, fake = _patch_grpc(monkeypatch)

	with pytest.raises(ValueError, match="when use_tls is false"):
		transport_module.GrpcUploadTransport("ingest", 50051, use_tls=False, **kwargs)
	assert fake.secure_calls == []
	assert fake.insecure_calls == []


@pytest.mark.parametrize(
	"kwargs",
	[
		{"client_cert_chain": b"cert"},
		{"client_private_key": b"key"},
	],
)
def test_transport_rejects_client_certificate_key_xor(monkeypatch, kwargs):
	transport_module, fake = _patch_grpc(monkeypatch)

	with pytest.raises(ValueError, match="must be supplied together"):
		transport_module.GrpcUploadTransport("ingest", 50051, use_tls=True, **kwargs)
	assert fake.secure_calls == []


def test_transport_requires_ca_for_client_identity(monkeypatch):
	transport_module, fake = _patch_grpc(monkeypatch)

	with pytest.raises(ValueError, match="mTLS requires root_certificates"):
		transport_module.GrpcUploadTransport(
			"ingest",
			50051,
			use_tls=True,
			client_cert_chain=b"cert",
			client_private_key=b"key",
		)
	assert fake.secure_calls == []


def test_server_auth_tls_accepts_custom_ca(monkeypatch):
	transport_module, fake = _patch_grpc(monkeypatch)

	transport_module.GrpcUploadTransport(
		"ingest.example", 50051, use_tls=True, root_certificates=b"ca"
	)
	assert fake.credential_calls == [{"root_certificates": b"ca"}]
	assert fake.secure_calls[0][:2] == ("ingest.example:50051", "credentials")
	assert fake.insecure_calls == []


def test_transport_resolves_finite_rpc_deadlines_from_environment(monkeypatch):
	transport_module, _fake = _patch_grpc(monkeypatch)
	monkeypatch.setenv("UPLOAD_RPC_TIMEOUT_SECONDS", "42.5")
	monkeypatch.setenv("RESUME_RPC_TIMEOUT_SECONDS", "3")

	transport = transport_module.GrpcUploadTransport(
		"ingest.example", 50051, use_tls=False
	)

	assert transport.upload_rpc_timeout_seconds == 42.5
	assert transport.resume_rpc_timeout_seconds == 3.0


@pytest.mark.parametrize("value", ["never", "0", "nan", "inf", "86401"])
def test_transport_rejects_invalid_rpc_deadlines(monkeypatch, value):
	transport_module, fake = _patch_grpc(monkeypatch)
	monkeypatch.setenv("UPLOAD_RPC_TIMEOUT_SECONDS", value)

	with pytest.raises(ValueError, match="UPLOAD_RPC_TIMEOUT_SECONDS"):
		transport_module.GrpcUploadTransport("ingest", 50051, use_tls=False)

	assert fake.insecure_calls == []


def test_data_plane_calls_pass_configured_deadlines(tmp_path, monkeypatch):
	import sys
	import robot_upload_agent.transport as transport_module

	class _Message:
		def __init__(self, **values):
			vars(self).update(values)

	fake_pb = SimpleNamespace(
		UploadMetadata=_Message,
		UploadSmallRequest=_Message,
		ResumeUploadRequest=_Message,
		UploadRequest=_Message,
		UploadChunk=_Message,
	)
	monkeypatch.setattr(transport_module, "pb", fake_pb)
	monkeypatch.setitem(sys.modules, "upload_pb2", fake_pb)

	class _Stub:
		def __init__(self):
			self.calls = []

		def UploadSmall(self, request, **kwargs):  # noqa: N802
			self.calls.append(("small", kwargs))
			return SimpleNamespace(status="completed")

		def ResumeUpload(self, request, **kwargs):  # noqa: N802
			self.calls.append(("resume", kwargs))
			return SimpleNamespace(resume_version=1, upload_id=request.upload_id, bytes_received=0, missing_chunks=[])

		def Upload(self, requests, **kwargs):  # noqa: N802
			list(requests)
			self.calls.append(("stream", kwargs))
			return SimpleNamespace(status="completed")

	path = tmp_path / "robot_data" / "rover-1" / "data.mcap"
	path.parent.mkdir(parents=True)
	path.write_bytes(b"payload")
	manifest = {
		"path": str(path),
		"file_size_bytes": path.stat().st_size,
		"upload_id": "upload-1",
		"sha256_hash": "a" * 64,
		"chunk_size_bytes": path.stat().st_size,
	}
	transport = object.__new__(transport_module.GrpcUploadTransport)
	transport.robot_id = "rover-1"
	transport.auth_metadata = (("authorization", "Bearer token"),)
	transport.upload_rpc_timeout_seconds = 41.0
	transport.resume_rpc_timeout_seconds = 7.0
	transport.stub = _Stub()

	transport.upload_small(manifest, b"payload")
	transport.upload_stream(manifest, [b"payload"])

	assert transport.stub.calls == [
		("small", {"metadata": transport.auth_metadata, "timeout": 41.0}),
		("resume", {"metadata": transport.auth_metadata, "timeout": 7.0}),
		("stream", {"metadata": transport.auth_metadata, "timeout": 41.0}),
	]


def test_mtls_transport_uses_complete_credential_set(monkeypatch):
	transport_module, fake = _patch_grpc(monkeypatch)

	transport_module.GrpcUploadTransport(
		"ingest.example",
		50051,
		use_tls=True,
		root_certificates=b"ca",
		client_cert_chain=b"cert",
		client_private_key=b"key",
	)
	assert fake.credential_calls == [
		{
			"root_certificates": b"ca",
			"private_key": b"key",
			"certificate_chain": b"cert",
		}
	]
	assert len(fake.secure_calls) == 1
	assert fake.insecure_calls == []


def test_queue_reads_all_mtls_files_before_building_transport(tmp_path, monkeypatch):
	ca = tmp_path / "ca.crt"
	cert = tmp_path / "robot.crt"
	key = tmp_path / "robot.key"
	ca.write_bytes(b"ca-bytes")
	cert.write_bytes(b"cert-bytes")
	key.write_bytes(b"key-bytes")
	config = tmp_path / "vault.json"
	config.write_text(
		"{"
		'"robot_id":"rover-7",'
		'"server":{"host":"ingest.example","port":7443},'
		'"tls":{"enabled":true,"mtls":true,'
		f'"ca_path":"{ca}","cert_path":"{cert}","key_path":"{key}"'
		"}"
		"}"
	)

	captured = {}

	class _CapturedTransport:
		def __init__(self, *args, **kwargs):
			captured["args"] = args
			captured["kwargs"] = kwargs

	import robot_upload_agent.core as core_module

	monkeypatch.setattr(core_module, "GrpcUploadTransport", _CapturedTransport)
	queue = UploadQueue(root=tmp_path, vault_config_path=config)
	queue._new_transport()

	assert captured["args"] == ("ingest.example", 7443, True)
	assert captured["kwargs"]["root_certificates"] == b"ca-bytes"
	assert captured["kwargs"]["client_cert_chain"] == b"cert-bytes"
	assert captured["kwargs"]["client_private_key"] == b"key-bytes"
	assert captured["kwargs"]["robot_id"] == "rover-7"
	assert "auth_token" not in captured["kwargs"]
