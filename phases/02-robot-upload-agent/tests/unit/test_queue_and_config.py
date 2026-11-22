from pathlib import Path

from robot_upload_agent.core import UploadAgentConfig, UploadQueue
from robot_storage_manager.core import save_manifest, load_or_create_manifest


# A complete MCAP envelope (leading magic, Footer record, closing magic).
# The upload gate treats a .mcap without its footer as still recording.
_MCAP_MAGIC = b"\x89MCAP0\r\n"
_MCAP_END = b"\x02" + (20).to_bytes(8, "little") + bytes(20) + _MCAP_MAGIC


def complete_mcap(payload: bytes) -> bytes:
	return _MCAP_MAGIC + payload + _MCAP_END



class DummyTransport:
	# The ingest server answers with the canonical UploadStatus "completed"
	# -- it never returned the manifest's old word "uploaded".
	def upload_small(self, manifest, data, compress=False):
		return {"status": "completed"}
	def upload_stream(self, manifest, chunks, compress=False):
		return {"status": "completed"}


def test_queue_describe_and_run_once(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	root.mkdir(parents=True)
	f = root / "a.mcap"
	f.write_bytes(complete_mcap(b"x"))
	mf = load_or_create_manifest(f)
	mf["status"] = "pending"
	save_manifest(mf)

	# min_stable_age_seconds=0: this test writes the file and uploads it in the
	# same instant, which the mtime stability gate would otherwise defer. The
	# gate itself is covered by test_upload_stability.py.
	q = UploadQueue(root=tmp_path / "robot", config=UploadAgentConfig(min_stable_age_seconds=0), transport=DummyTransport())
	desc = q.describe()
	assert desc["pending_count"] == 1

	res0 = q.run_once()
	assert res0["uploaded"].endswith("a.mcap")
	mf2 = load_or_create_manifest(f)
	# The manifest records the canonical UploadStatus value.
	assert mf2["status"] == "completed"
	assert mf2["upload_id"]
	
	# Second attempt should find it already uploaded
	res = q.run_once()
	assert res["uploaded"] is None  # Already uploaded

	# Drain several files at once
	res_many = q.run_many(max_files=2)
	assert isinstance(res_many["uploaded"], list)


def test_run_many_processes_multiple_files(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	root.mkdir(parents=True)
	# Create two files
	f1 = root / "a1.mcap"; f1.write_bytes(complete_mcap(b"x"))
	f2 = root / "a2.mcap"; f2.write_bytes(complete_mcap(b"x"))
	from robot_storage_manager.core import load_or_create_manifest, save_manifest
	for f in (f1, f2):
		mf = load_or_create_manifest(f); mf["status"] = "pending"; save_manifest(mf)

	class _DT:
		def upload_small(self, manifest, data, compress=False): return {"status": "completed"}
		def upload_stream(self, manifest, chunks, compress=False): return {"status": "completed"}

	# See test_queue_describe_and_run_once for min_stable_age_seconds=0.
	cfg = UploadAgentConfig(concurrency=2, min_stable_age_seconds=0)
	q = UploadQueue(root=tmp_path / "robot", config=cfg)
	q.transport = _DT()
	res = q.run_many()
	assert len(res["uploaded"]) == 2


def test_bandwidth_throttle_uses_queue_limiter(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	root.mkdir(parents=True)
	big = root / "b.mcap"
	big.write_bytes(complete_mcap(b"x" * (200 * 1024)))  # ~200 KiB to stream
	from robot_storage_manager.core import load_or_create_manifest, save_manifest
	mf = load_or_create_manifest(big); mf["status"] = "pending"; save_manifest(mf)

	calls = []
	class Limiter:
		def acquire(self, count): calls.append(count)

	class _DT2:
		def upload_small(self, manifest, data, compress=False): return {"status": "completed"}
		def upload_stream(self, manifest, chunks, compress=False):
			# exhaust generator
			for _ in chunks: pass
			return {"status": "completed"}

	cfg = UploadAgentConfig(small_file_threshold_mb=0, chunk_size_bytes=5 * 1024 * 1024, bandwidth_mbps=1, min_stable_age_seconds=0)
	q = UploadQueue(root=tmp_path / "robot", config=cfg)
	q.bandwidth_limiter = Limiter()
	q.transport = _DT2()
	res = q.run_once()
	assert res["uploaded"].endswith("b.mcap")
	assert calls == [big.stat().st_size]
