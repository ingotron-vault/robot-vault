"""File-level upload concurrency.

Three properties matter and each has a failure mode that would corrupt data or
silently waste the feature:

1. The bound. `parallel_streams` is a request; the effective count is the min of
   the operator's value, the connection strategy's value, the server's advertised
   per-robot limit and MAX_PARALLEL_STREAMS. Over-requesting does not fail, it
   blocks in the server's `acquire_per_robot` while holding an idle channel.
2. The unit of concurrency is a WHOLE FILE. The server validates
   `chunk_number == expected_next` per upload, so a design that split one
   file across streams would be rejected -- and one upload_id must never be in
   flight twice (it would race on one S3 key and one `uploads` row).
3. Independent channels. N streams on ONE gRPC channel are N HTTP/2 streams on
   one TCP connection sharing one congestion window, which is worth ~nothing on
   the link-limited path this feature exists to help.
"""
import threading

import pytest
from pathlib import Path

from robot_storage_manager.core import load_or_create_manifest, save_manifest

from robot_vault_config.model import MAX_PARALLEL_STREAMS
from robot_upload_agent.core import ASSUMED_SERVER_PER_ROBOT_STREAMS, UploadAgentConfig, UploadQueue
from robot_upload_agent.smart_batching import ConnectionQuality


# A complete MCAP envelope (leading magic, Footer record, closing magic).
# The upload gate treats a .mcap without its footer as still recording.
_MCAP_MAGIC = b"\x89MCAP0\r\n"
_MCAP_END = b"\x02" + (20).to_bytes(8, "little") + bytes(20) + _MCAP_MAGIC


def complete_mcap(payload: bytes) -> bytes:
	return _MCAP_MAGIC + payload + _MCAP_END



class ConcurrencyProbeTransport:
	"""Records peak overlap and the (upload_id, chunk_number) sequence per file."""

	def __init__(self, per_robot_limit=None):
		self.lock = threading.Lock()
		self.active = 0
		self.peak = 0
		self.chunk_order = {}
		self.calls = []
		self._per_robot_limit = per_robot_limit
		self.gate = threading.Barrier(1)

	def server_per_robot_stream_limit(self):
		return self._per_robot_limit

	def _enter(self):
		with self.lock:
			self.active += 1
			self.peak = max(self.peak, self.active)

	def _exit(self):
		with self.lock:
			self.active -= 1

	def upload_small(self, manifest, data, compress=False):
		self._enter()
		try:
			with self.lock:
				self.calls.append(manifest.get("upload_id"))
			return {"status": "completed"}
		finally:
			self._exit()

	def upload_stream(self, manifest, chunks, compress=False):
		self._enter()
		try:
			seen = []
			for i, buf in enumerate(chunks):
				seen.append((i, len(buf)))
			with self.lock:
				self.calls.append(manifest.get("upload_id"))
				self.chunk_order[manifest.get("upload_id")] = seen
			return {"status": "completed"}
		finally:
			self._exit()


def _pending(root: Path, name: str, size: int = 1024) -> Path:
	root.mkdir(parents=True, exist_ok=True)
	p = root / name
	p.write_bytes(complete_mcap(b"z" * size))
	mf = load_or_create_manifest(p)
	mf["status"] = "pending"
	save_manifest(mf)
	return p


def _queue(tmp_path, transport, **cfg_kwargs):
	cfg = UploadAgentConfig(min_stable_age_seconds=0, **cfg_kwargs)
	q = UploadQueue(root=tmp_path / "robot", config=cfg, transport=transport)
	# Pin the detected quality so the strategy's parallel_streams is not a
	# function of the test box's path to the configured server.
	q.smart_batcher._cached_quality = ConnectionQuality.POOR  # strategy value 4
	q.smart_batcher._last_quality_check = float("inf")
	return q


# --- the bound ---------------------------------------------------------------

def test_effective_streams_is_the_min_of_every_bound(tmp_path: Path):
	t = ConcurrencyProbeTransport(per_robot_limit=8)
	q = _queue(tmp_path, t, parallel_streams=3)
	# strategy(POOR)=4, config=3, server=8, MAX=4, work=10 -> 3
	assert q._resolve_parallel_streams(10) == 3
	# work_items is also a bound: never more workers than files.
	assert q._resolve_parallel_streams(2) == 2


def test_config_ceiling_is_clamped_to_max_parallel_streams():
	cfg = UploadAgentConfig(parallel_streams=99)
	assert cfg.parallel_streams == MAX_PARALLEL_STREAMS
	assert UploadAgentConfig(parallel_streams=0).parallel_streams == 1


def test_server_advertised_limit_caps_the_client(tmp_path: Path):
	"""Exceeding PER_ROBOT_CONCURRENCY does not fail -- it blocks in
	`acquire_per_robot` while the extra worker holds a channel and burns its
	deadline. So the server's number wins."""
	t = ConcurrencyProbeTransport(per_robot_limit=1)
	q = _queue(tmp_path, t, parallel_streams=4)
	assert q._resolve_parallel_streams(10) == 1


def test_falls_back_to_the_documented_limit_when_the_server_advertises_nothing(tmp_path: Path):
	t = ConcurrencyProbeTransport(per_robot_limit=None)
	q = _queue(tmp_path, t, parallel_streams=4)
	assert q._resolve_parallel_streams(10) == ASSUMED_SERVER_PER_ROBOT_STREAMS


def test_the_server_is_asked_only_once(tmp_path: Path):
	calls = {"n": 0}
	t = ConcurrencyProbeTransport(per_robot_limit=2)

	def counted():
		calls["n"] += 1
		return 2

	t.server_per_robot_stream_limit = counted
	q = _queue(tmp_path, t, parallel_streams=4)
	for _ in range(5):
		q._resolve_parallel_streams(10)
	assert calls["n"] == 1


def test_a_slow_or_broken_healthcheck_does_not_break_uploads(tmp_path: Path):
	t = ConcurrencyProbeTransport()

	def boom():
		raise RuntimeError("HealthCheck unavailable")

	t.server_per_robot_stream_limit = boom
	q = _queue(tmp_path, t, parallel_streams=4)
	assert q._resolve_parallel_streams(10) == ASSUMED_SERVER_PER_ROBOT_STREAMS


# --- observed concurrency ----------------------------------------------------

def test_backlog_drain_overlaps_up_to_the_bound_and_no_further(tmp_path: Path):
	"""The backlog-drain claim: N whole files in flight at once."""
	root = tmp_path / "robot" / "2026-07-25"
	for i in range(6):
		_pending(root, f"f{i}.mcap")
	t = ConcurrencyProbeTransport(per_robot_limit=2)
	q = _queue(tmp_path, t, parallel_streams=2, small_file_threshold_mb=1)

	res = q.run_many(max_files=0)  # 0 == everything pending

	assert len(res["uploaded"]) == 6
	assert res["errors"] == []
	assert t.peak <= 2, f"exceeded the bound: peak={t.peak}"


def test_a_single_stream_bound_stays_sequential(tmp_path: Path):
	root = tmp_path / "robot" / "2026-07-25"
	for i in range(4):
		_pending(root, f"g{i}.mcap")
	t = ConcurrencyProbeTransport(per_robot_limit=1)
	q = _queue(tmp_path, t, parallel_streams=1, small_file_threshold_mb=1)

	res = q.run_many(max_files=0)

	assert len(res["uploaded"]) == 4
	assert t.peak == 1


# --- chunk order and resume safety -----------------------------------------

def test_chunks_of_one_file_are_never_split_or_reordered(tmp_path: Path):
	"""The server accepts chunk_number == expected_next and nothing else.
	Concurrency is per FILE, so each file's stream still arrives 0,1,2,... on one
	RPC."""
	root = tmp_path / "robot" / "2026-07-25"
	for i in range(3):
		_pending(root, f"h{i}.mcap", size=200 * 1024)
	t = ConcurrencyProbeTransport(per_robot_limit=2)
	q = _queue(tmp_path, t, parallel_streams=2, small_file_threshold_mb=0,
	           chunk_size_bytes=5 * 1024 * 1024)

	res = q.run_many(max_files=0)

	assert len(res["uploaded"]) == 3
	for upload_id, seen in t.chunk_order.items():
		assert [n for n, _ in seen] == list(range(len(seen))), upload_id


def test_upload_selected_dedupes_by_path_and_upload_id(tmp_path: Path):
	"""Two concurrent streams carrying the same upload_id would race on one S3
	key and one `uploads` row, because the server calls create_multipart_upload
	unconditionally per stream."""
	root = tmp_path / "robot" / "2026-07-25"
	f = _pending(root, "dup.mcap")
	t = ConcurrencyProbeTransport(per_robot_limit=2)
	q = _queue(tmp_path, t, parallel_streams=2, small_file_threshold_mb=1)

	res = q.upload_selected([f, f, f])

	assert res["uploaded"] == [str(f)]
	assert len(t.calls) == 1, f"one file must be uploaded once, got {t.calls}"


def test_each_worker_gets_an_independent_channel(tmp_path: Path, monkeypatch):
	"""The physics finding: a gRPC channel opens ONE TCP connection, so N streams
	on a shared channel share one congestion window and buy nothing on a
	link-limited robot. Each worker must therefore build its own transport."""
	root = tmp_path / "robot" / "2026-07-25"
	for i in range(4):
		_pending(root, f"k{i}.mcap")

	built = []
	closed = []

	class FakeChannelTransport(ConcurrencyProbeTransport):
		def __init__(self):
			super().__init__(per_robot_limit=2)
			built.append(self)

		def close(self):
			closed.append(self)

	q = _queue(tmp_path, None, parallel_streams=2, small_file_threshold_mb=1)
	monkeypatch.setattr(q, "_new_transport", FakeChannelTransport)

	res = q.run_many(max_files=0)

	assert len(res["uploaded"]) == 4
	# One transport per worker thread (plus at most one throwaway used for the
	# HealthCheck probe), and every one of them closed afterwards.
	assert len(built) >= 2, built
	assert len(set(id(b) for b in built)) == len(built), "channels must not be shared"
	assert len(closed) == len(built), "every worker channel must be closed"


@pytest.mark.parametrize(
	("quality", "expected_indices"),
	[(ConnectionQuality.POOR, [2, 0, 1]), (ConnectionQuality.EXCELLENT, [2, 1, 0])],
)
def test_selected_uploads_keep_priority_and_link_size_order(tmp_path: Path, quality, expected_indices):
	root = tmp_path / "robot"
	files = [
		_pending(root, "small.mcap", 100),
		_pending(root, "large.mcap", 300),
		_pending(root, "critical.mcap", 200),
	]
	critical_manifest = load_or_create_manifest(files[2])
	critical_manifest["priority"] = "critical"
	save_manifest(critical_manifest)

	transport = ConcurrencyProbeTransport(per_robot_limit=1)
	queue = _queue(tmp_path, transport, parallel_streams=1)
	queue.smart_batcher._cached_quality = quality
	result = queue.upload_selected(files)

	assert result["errors"] == []
	ids = [load_or_create_manifest(path)["upload_id"] for path in files]
	assert transport.calls == [ids[index] for index in expected_indices]
