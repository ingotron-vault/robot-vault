import hashlib
from pathlib import Path

from robot_upload_agent.core import UploadAgentConfig, UploadQueue, ensure_sha256_hash
from robot_storage_manager.core import load_or_create_manifest, save_manifest


# A complete MCAP envelope (leading magic, Footer record, closing magic).
# The upload gate treats a .mcap without its footer as still recording.
_MCAP_MAGIC = b"\x89MCAP0\r\n"
_MCAP_END = b"\x02" + (20).to_bytes(8, "little") + bytes(20) + _MCAP_MAGIC


def complete_mcap(payload: bytes) -> bytes:
	return _MCAP_MAGIC + payload + _MCAP_END



class HashCapturingTransport:
	"""Fake transport that mirrors how the real GrpcUploadTransport derives
	expected_sha256 (transport.py:_metadata_from_manifest reads
	manifest.get("sha256_hash") or ""), so tests can assert on the value that
	would actually be put on the wire.
	"""
	def __init__(self):
		self.calls = []

	def _record(self, kind, manifest, size):
		self.calls.append({
			"kind": kind,
			"expected_sha256": manifest.get("sha256_hash") or "",
			"size": size,
		})

	def upload_small(self, manifest, data, compress=False):
		self._record("small", manifest, len(data))
		return {"status": "completed"}

	def upload_stream(self, manifest, chunks, compress=False):
		n = 0
		for c in chunks:
			n += len(c)
		self._record("stream", manifest, n)
		return {"status": "completed"}


def test_run_once_computes_and_sends_sha256_when_missing(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	root.mkdir(parents=True)
	f = root / "a.mcap"
	content = complete_mcap(b"integrity-check-payload" * 1000)
	f.write_bytes(content)
	expected_digest = hashlib.sha256(content).hexdigest()

	mf = load_or_create_manifest(f)
	assert not mf.get("sha256_hash")  # no hash until the queue computes one
	mf["status"] = "pending"
	save_manifest(mf)

	transport = HashCapturingTransport()
	# small_file_threshold_mb=48 keeps this ~23 KB file on the upload_small
	# path (48 is the ceiling: UploadSmall sends the whole file in one
	# gRPC message and both ends cap a message at 50 MiB, so a threshold of 100
	# is rejected at construction);
	# min_stable_age_seconds=0 opts out of the mtime stability gate, which
	# would otherwise defer a file written this instant (see
	# test_upload_stability.py for the gate's own coverage).
	cfg = UploadAgentConfig(small_file_threshold_mb=48, min_stable_age_seconds=0)
	q = UploadQueue(root=tmp_path / "robot", config=cfg, transport=transport)

	res = q.run_once()

	assert res["uploaded"] == str(f)
	assert len(transport.calls) == 1
	assert transport.calls[0]["kind"] == "small"
	assert transport.calls[0]["expected_sha256"] != ""
	assert transport.calls[0]["expected_sha256"] == expected_digest

	# The computed hash must also be persisted so a later run doesn't lose it.
	mf2 = load_or_create_manifest(f)
	assert mf2["sha256_hash"] == expected_digest


def test_upload_selected_streaming_path_sends_real_sha256_with_empty_hash(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	root.mkdir(parents=True)
	f = root / "b.mcap"
	content = complete_mcap(b"large-file-chunked-payload-" * 5000)
	f.write_bytes(content)
	expected_digest = hashlib.sha256(content).hexdigest()

	mf = load_or_create_manifest(f)
	mf["sha256_hash"] = ""  # explicit empty string, not just missing
	mf["status"] = "pending"
	save_manifest(mf)

	transport = HashCapturingTransport()
	cfg = UploadAgentConfig(small_file_threshold_mb=0, chunk_size_bytes=5 * 1024 * 1024, bandwidth_mbps=1000, min_stable_age_seconds=0)
	q = UploadQueue(root=tmp_path / "robot", config=cfg, transport=transport)

	res = q.upload_selected([f])

	assert res["uploaded"] == [str(f)]
	assert len(transport.calls) == 1
	assert transport.calls[0]["kind"] == "stream"
	assert transport.calls[0]["expected_sha256"] != ""
	assert transport.calls[0]["expected_sha256"] == expected_digest


def test_ensure_sha256_hash_persists_and_is_idempotent(tmp_path: Path):
	f = tmp_path / "c.mcap"
	content = complete_mcap(b"idempotent-hash-check")
	f.write_bytes(content)
	expected_digest = hashlib.sha256(content).hexdigest()

	mf = load_or_create_manifest(f)
	assert mf.get("sha256_hash") is None

	mf = ensure_sha256_hash(f, mf)
	assert mf["sha256_hash"] == expected_digest

	# Persisted to disk via save_manifest.
	mf_reloaded = load_or_create_manifest(f)
	assert mf_reloaded["sha256_hash"] == expected_digest

	# A manifest that already carries a real hash is left untouched.
	mf_reloaded["sha256_hash"] = "already-computed"
	mf_again = ensure_sha256_hash(f, mf_reloaded)
	assert mf_again["sha256_hash"] == "already-computed"
