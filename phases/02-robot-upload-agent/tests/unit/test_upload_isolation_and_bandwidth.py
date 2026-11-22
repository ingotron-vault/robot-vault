import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from robot_storage_manager.core import load_or_create_manifest, manifest_path_for, save_manifest
from robot_upload_agent.bandwidth import BandwidthLimiter, UploadCancelled
from robot_upload_agent.cli import upload_exit_status
from robot_upload_agent.core import UploadAgentConfig, UploadQueue
from robot_upload_agent.smart_batching import ConnectionQuality


# A complete MCAP envelope (leading magic, Footer record, closing magic).
# The upload gate treats a .mcap without its footer as still recording.
_MCAP_MAGIC = b"\x89MCAP0\r\n"
_MCAP_END = b"\x02" + (20).to_bytes(8, "little") + bytes(20) + _MCAP_MAGIC


def complete_mcap(payload: bytes) -> bytes:
    return _MCAP_MAGIC + payload + _MCAP_END


def mcap_of_size(size: int, fill: bytes = b"x") -> bytes:
    """A complete MCAP of exactly `size` bytes, for byte-budget assertions."""
    return complete_mcap(fill * (size - len(complete_mcap(b""))))



class Transport:
    def __init__(self):
        self.sent = []

    def upload_small(self, manifest, data):
        self.sent.append((manifest["path"], len(data)))
        return {"status": "completed"}

    def upload_stream(self, manifest, chunks):
        for data in chunks:
            self.sent.append((manifest["path"], len(data)))
        return {"status": "completed"}

    def server_per_robot_stream_limit(self):
        return 4


def _files(tmp_path, count=4):
    root = tmp_path / "robot"
    root.mkdir()
    files = []
    for i in range(count):
        path = root / f"{i}.mcap"
        path.write_bytes(complete_mcap(b"x" * 128))
        files.append(path)
    return root, files


@pytest.mark.parametrize("bad_index", [0, 1, 3])
def test_run_many_drains_prepared_work_after_file_failure(tmp_path, monkeypatch, bad_index):
    root, files = _files(tmp_path)
    transport = Transport()
    queue = UploadQueue(root, UploadAgentConfig(min_stable_age_seconds=0), transport=transport)
    original = queue._prepare

    def prepare(path):
        if path == files[bad_index]:
            raise OSError("recording disappeared during hashing")
        return original(path)

    monkeypatch.setattr(queue, "_prepare", prepare)
    result = queue.run_many(max_files=0)
    assert result["uploaded"] == [str(path) for path in files if path != files[bad_index]]
    assert result["errors"] == [{"file": str(files[bad_index]), "error": {
        "status": "preparation_failed", "detail": "recording disappeared during hashing"}}]
    assert len(transport.sent) == 3


def test_run_once_continues_after_preparation_failure(tmp_path, monkeypatch):
    root, files = _files(tmp_path, 2)
    queue = UploadQueue(root, UploadAgentConfig(min_stable_age_seconds=0), transport=Transport())
    original = queue._prepare
    monkeypatch.setattr(queue, "_prepare", lambda path: (_ for _ in ()).throw(ValueError("changed")) if path == files[0] else original(path))
    result = queue.run_once()
    assert result["uploaded"] == str(files[1])
    assert result["errors"][0]["file"] == str(files[0])


def test_selected_keeps_good_results_and_missing_file_error(tmp_path):
    root, files = _files(tmp_path, 2)
    queue = UploadQueue(root, UploadAgentConfig(min_stable_age_seconds=0), transport=Transport())
    missing = root / "gone.mcap"
    result = queue.upload_selected([files[0], missing, files[1]])
    assert result["uploaded"] == [str(files[0]), str(files[1])]
    assert result["errors"] == [{"file": str(missing), "error": {"status": "missing"}}]


def test_malformed_manifest_is_recovered_without_changing_upload_identity(tmp_path):
    root, files = _files(tmp_path, 2)
    manifest_path_for(files[0]).write_text("[]")
    queue = UploadQueue(root, UploadAgentConfig(min_stable_age_seconds=0), transport=Transport())
    result = queue.run_many(max_files=0)
    assert result["errors"] == []
    assert len(result["uploaded"]) == 2
    assert manifest_path_for(files[0]).with_name(files[0].name + ".manifest.json.corrupt").exists()
    assert load_or_create_manifest(files[0])["upload_id"]


def test_unexpected_preparation_bug_propagates(tmp_path, monkeypatch):
    root, _ = _files(tmp_path, 2)
    queue = UploadQueue(root, UploadAgentConfig(min_stable_age_seconds=0), transport=Transport())
    monkeypatch.setattr(queue, "_prepare", lambda _: (_ for _ in ()).throw(TypeError("programming bug")))
    with pytest.raises(TypeError, match="programming bug"):
        queue.run_many(max_files=0)


class VirtualClock:
    def __init__(self):
        self.now = 0.0
        self.lock = threading.Lock()

    def clock(self):
        with self.lock:
            return self.now

    def wait(self, seconds):
        with self.lock:
            self.now += seconds


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_workers_share_one_aggregate_allowance(workers):
    virtual = VirtualClock()
    limiter = BandwidthLimiter(1, clock=virtual.clock, wait=virtual.wait)
    payload = 128 * 1024  # one second of binary-Mbit/s allowance
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(limiter.acquire, [payload] * 4))
    assert virtual.clock() == pytest.approx(3, abs=0.1)


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_queue_workers_share_allowance_for_unary_payloads(tmp_path, workers):
    root, files = _files(tmp_path)
    for path in files:
        path.write_bytes(mcap_of_size(128 * 1024, b"x"))
    transport = Transport()
    queue = UploadQueue(root, UploadAgentConfig(
        min_stable_age_seconds=0, parallel_streams=workers,
        small_file_threshold_mb=1, bandwidth_mbps=1,
    ), transport=transport)
    queue.smart_batcher._cached_quality = ConnectionQuality.POOR
    queue.smart_batcher._last_quality_check = float("inf")
    virtual = VirtualClock()
    queue.bandwidth_limiter = BandwidthLimiter(1, clock=virtual.clock, wait=virtual.wait)
    result = queue.run_many(max_files=0)
    assert len(result["uploaded"]) == 4
    assert result["effective_parallel_streams"] == workers
    assert sum(size for _, size in transport.sent) == 4 * 128 * 1024
    assert virtual.clock() == pytest.approx(3, abs=0.1)


def test_unary_streaming_retries_and_sparse_resume_use_same_budget(tmp_path):
    root, files = _files(tmp_path, 1)
    virtual = VirtualClock()
    limiter = BandwidthLimiter(1, clock=virtual.clock, wait=virtual.wait)
    limiter.acquire(64 * 1024)  # unary attempt
    limiter.acquire(64 * 1024)  # retransmission
    limiter.acquire(128 * 1024)  # only missing streamed part
    assert virtual.clock() == pytest.approx(1, abs=0.1)


def test_mixed_unary_and_streaming_queue_throughput(tmp_path):
    root, files = _files(tmp_path, 2)
    files[0].write_bytes(mcap_of_size(128 * 1024, b"s"))
    files[1].write_bytes(mcap_of_size(2 * 1024 * 1024, b"l"))
    transport = Transport()
    queue = UploadQueue(root, UploadAgentConfig(
        min_stable_age_seconds=0, small_file_threshold_mb=1, bandwidth_mbps=1,
    ), transport=transport)
    queue.smart_batcher._cached_quality = ConnectionQuality.POOR
    queue.smart_batcher._last_quality_check = float("inf")
    virtual = VirtualClock()
    queue.bandwidth_limiter = BandwidthLimiter(1, clock=virtual.clock, wait=virtual.wait)
    result = queue.run_many(max_files=0)
    assert result["errors"] == []
    assert sorted(size for _, size in transport.sent) == [128 * 1024, 2 * 1024 * 1024]
    assert virtual.clock() == pytest.approx(16, abs=0.1)


def test_retry_retransmission_consumes_allowance(tmp_path):
    root, files = _files(tmp_path, 1)
    files[0].write_bytes(mcap_of_size(128 * 1024, b"x"))

    class Code:
        name = "UNAVAILABLE"

    class TemporaryFailure(Exception):
        def code(self):
            return Code()

    class RetryTransport(Transport):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def upload_stream(self, manifest, chunks):
            self.attempts += 1
            for data in chunks:
                self.sent.append((manifest["path"], len(data)))
            if self.attempts == 1:
                raise TemporaryFailure("link lost after sending")
            return {"status": "completed"}

    transport = RetryTransport()
    queue = UploadQueue(root, UploadAgentConfig(
        min_stable_age_seconds=0, small_file_threshold_mb=0, bandwidth_mbps=1,
        max_retries=1, base_backoff_seconds=0,
    ), transport=transport)
    virtual = VirtualClock()
    queue.bandwidth_limiter = BandwidthLimiter(1, clock=virtual.clock, wait=virtual.wait)
    result = queue.run_once()
    assert result["uploaded"] == str(files[0])
    assert transport.attempts == 2
    assert [size for _, size in transport.sent] == [128 * 1024] * 2
    assert virtual.clock() == pytest.approx(1, abs=0.1)


def test_unlimited_and_cancelled_wait():
    virtual = VirtualClock()
    unlimited = BandwidthLimiter(0, clock=virtual.clock, wait=virtual.wait)
    unlimited.acquire(100 * 1024 * 1024)
    assert virtual.clock() == 0
    cancel = threading.Event()

    def wait(_):
        cancel.set()

    limiter = BandwidthLimiter(1, clock=virtual.clock, wait=wait, cancel_event=cancel)
    with pytest.raises(UploadCancelled):
        limiter.acquire(2 * 128 * 1024)


def test_one_second_allowance_and_whole_message_packetization():
    virtual = VirtualClock()
    limiter = BandwidthLimiter(1, clock=virtual.clock, wait=virtual.wait)
    assert limiter.burst_bytes == 128 * 1024
    limiter.acquire(2 * 128 * 1024)
    assert virtual.clock() == pytest.approx(1, abs=0.1)
    limiter.acquire(128 * 1024)
    assert virtual.clock() == pytest.approx(2, abs=0.1)


def test_keyboard_interrupt_wakes_other_paced_workers(tmp_path, monkeypatch):
    root, files = _files(tmp_path, 2)
    queue = UploadQueue(root, UploadAgentConfig(
        min_stable_age_seconds=0, parallel_streams=2, bandwidth_mbps=1,
    ), transport=Transport())
    queue.smart_batcher._cached_quality = ConnectionQuality.POOR
    queue.smart_batcher._last_quality_check = float("inf")
    barrier = threading.Barrier(2)

    def upload(path, manifest, on_progress=None):
        barrier.wait(timeout=2)
        if path == files[0]:
            raise KeyboardInterrupt
        queue.bandwidth_limiter.acquire(100 * 128 * 1024)
        return str(path), None

    monkeypatch.setattr(queue, "_upload_one", upload)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        queue._drain([(path, {}) for path in files])
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("kind,expected", [("empty", 0), ("deferred", 0), ("failed", 1)])
def test_real_cli_subprocess_exit_status(tmp_path, kind, expected):
    root = tmp_path / "robot"
    root.mkdir()
    if kind != "empty":
        path = root / "recording.mcap"
        path.write_bytes(complete_mcap(b"data"))
        if kind == "failed":
            manifest_path_for(path).mkdir()  # unreadable manifest sidecar
    source_roots = [
        Path(__file__).resolve().parents[3] / "common-config" / "src",
        Path(__file__).resolve().parents[3] / "01-robot-storage" / "src",
        Path(__file__).resolve().parents[2] / "src",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(map(str, source_roots))
    command = [sys.executable, "-c", "from robot_upload_agent.cli import main; raise SystemExit(main())",
               "upload", "--all", "--root", str(root)]
    completed = subprocess.run(command, env=env, capture_output=True, text=True)
    assert completed.returncode == expected, completed.stderr
    payload = json.loads(completed.stdout)
    assert bool(payload["errors"]) == bool(expected)


@pytest.mark.parametrize("result,expected", [
    ({"uploaded": None, "skipped": []}, 0),
    ({"uploaded": None, "skipped": ["recording"]}, 0),
    ({"uploaded": "good", "errors": []}, 0),
    ({"uploaded": "good", "errors": [{"file": "bad"}]}, 1),
    ({"uploaded": None, "error": {"status": "exception"}}, 1),
])
def test_upload_exit_status_decision_table(result, expected):
    assert upload_exit_status(result) == expected


@pytest.mark.parametrize("single,result,expected", [
    (True, {"uploaded": "good", "skipped": [], "errors": [{"file": "bad", "error": {"status": "missing"}}]}, 1),
    (False, {"uploaded": ["good"], "skipped": [], "errors": [{"file": "bad", "error": {"status": "exception"}}]}, 1),
    (False, {"uploaded": [], "skipped": ["recording"], "errors": []}, 0),
])
def test_cli_entrypoint_subprocess_classifies_single_and_mixed_results(tmp_path, single, result, expected):
    phase_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([
        str(phase_root / "common-config" / "src"),
        str(phase_root / "01-robot-storage" / "src"),
        str(phase_root / "02-robot-upload-agent" / "src"),
    ])
    script = (
        "import json, robot_upload_agent.cli as cli\n"
        "class Queue:\n"
        " def __init__(self, **kwargs): pass\n"
        " def run_once(self, **kwargs): return json.loads('" + json.dumps(result) + "')\n"
        " def run_many(self, **kwargs): return json.loads('" + json.dumps(result) + "')\n"
        "cli.UploadQueue = Queue\n"
        "raise SystemExit(cli.main())\n"
    )
    command = [sys.executable, "-c", script, "upload", "--root", str(tmp_path)]
    if not single:
        command.append("--all")
    completed = subprocess.run(command, env=env, capture_output=True, text=True)
    assert completed.returncode == expected, completed.stderr
    assert json.loads(completed.stdout) == result
