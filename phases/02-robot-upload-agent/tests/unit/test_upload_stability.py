"""Coverage for the file-stability gate on upload-candidate selection.

A robot that is still recording keeps extending its .mcap, so hashing and
uploading it now ships a partial file under a sha256 that stops matching as
soon as the recorder appends again. UploadQueue._is_stable (phase-01's
is_file_stable) gates run_once/run_many/upload_selected on the file's mtime
and, for .mcap files, on a complete MCAP footer.

Tests here get "an old file" either by back-dating the real mtime (os.utime)
or by injecting a clock that reads later than wall time -- never by sleeping.
"""
import os
import time
from pathlib import Path

from robot_storage_manager.core import (
	DEFAULT_STABILITY_SECONDS,
	load_or_create_manifest,
	save_manifest,
)
from robot_upload_agent.core import UploadAgentConfig, UploadQueue


class RecordingTransport:
	def __init__(self):
		self.calls = []

	def upload_small(self, manifest, data, compress=False):
		self.calls.append(manifest.get("path"))
		return {"status": "completed"}

	def upload_stream(self, manifest, chunks, compress=False):
		for _ in chunks:
			pass
		self.calls.append(manifest.get("path"))
		return {"status": "completed"}


MCAP_MAGIC = b"\x89MCAP0\r\n"
MCAP_FOOTER = b"\x02" + (20).to_bytes(8, "little") + bytes(20)
COMPLETE_MCAP = MCAP_MAGIC + b"telemetry" + MCAP_FOOTER + MCAP_MAGIC


def _pending_file(
	root: Path, name: str = "a.mcap", age_seconds: float = 0.0, content: bytes = COMPLETE_MCAP
) -> Path:
	"""Create a pending .mcap whose mtime sits `age_seconds` in the past."""
	root.mkdir(parents=True, exist_ok=True)
	f = root / name
	f.write_bytes(content)
	if age_seconds:
		past = time.time() - age_seconds
		os.utime(f, (past, past))
	mf = load_or_create_manifest(f)
	mf["status"] = "pending"
	save_manifest(mf)
	return f


def _advanced_clock(seconds: float):
	"""A clock reading `seconds` ahead of wall time (the file looks that old)."""
	return lambda: time.time() + seconds


def test_run_once_skips_a_file_that_is_still_being_written(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	f = _pending_file(root)  # mtime == now

	transport = RecordingTransport()
	# "Now" is the file's own mtime, so a slow host cannot age it past the window.
	written = f.stat().st_mtime
	q = UploadQueue(root=tmp_path / "robot", config=UploadAgentConfig(), transport=transport, clock=lambda: written)
	res = q.run_once()

	assert res["uploaded"] is None
	assert res["skipped"] == [str(f)]
	assert transport.calls == []
	# Not even hashed: the content is still changing, so a hash now is garbage.
	mf = load_or_create_manifest(f)
	assert mf["status"] == "pending"
	assert not mf["sha256_hash"]


def test_run_once_uploads_a_file_older_than_the_stability_window(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	f = _pending_file(root, age_seconds=DEFAULT_STABILITY_SECONDS + 60)

	transport = RecordingTransport()
	q = UploadQueue(root=tmp_path / "robot", config=UploadAgentConfig(), transport=transport)
	res = q.run_once()

	assert res["uploaded"] == str(f)
	assert res["skipped"] == []
	assert transport.calls == [str(f)]
	# Canonical UploadStatus value in the manifest.
	assert load_or_create_manifest(f)["status"] == "completed"


def test_injected_clock_advances_a_fresh_file_past_the_window(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	f = _pending_file(root)  # mtime == now

	transport = RecordingTransport()
	q = UploadQueue(
		root=tmp_path / "robot",
		config=UploadAgentConfig(),
		transport=transport,
		clock=_advanced_clock(DEFAULT_STABILITY_SECONDS + 60),
	)
	res = q.run_once()

	assert res["uploaded"] == str(f)
	assert transport.calls == [str(f)]


def test_run_many_skips_only_the_unstable_file(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	settled = _pending_file(root, name="settled.mcap", age_seconds=DEFAULT_STABILITY_SECONDS + 60)
	recording = _pending_file(root, name="recording.mcap")

	transport = RecordingTransport()
	q = UploadQueue(
		root=tmp_path / "robot",
		config=UploadAgentConfig(concurrency=5),
		transport=transport,
	)
	res = q.run_many()

	assert res["uploaded"] == [str(settled)]
	assert res["skipped"] == [str(recording)]
	assert res["errors"] == []
	assert load_or_create_manifest(recording)["status"] == "pending"


def test_upload_selected_rejects_a_file_that_is_still_being_written(tmp_path: Path):
	"""An explicit UI selection is gated too, but reported as a per-file error.

	The operator picked a file, not a moment, and can't see that the robot is
	mid-write. Unlike the scan loops there is no "next scan" to retry silently,
	so the job store / UI has to be told why nothing happened.
	"""
	root = tmp_path / "robot" / "2025-08-14"
	f = _pending_file(root)

	transport = RecordingTransport()
	written = f.stat().st_mtime  # "now" is the write time, however slow the host is
	q = UploadQueue(root=tmp_path / "robot", config=UploadAgentConfig(), transport=transport, clock=lambda: written)
	res = q.upload_selected([f])

	assert res["uploaded"] == []
	assert len(res["errors"]) == 1
	assert res["errors"][0]["file"] == str(f)
	assert res["errors"][0]["error"]["status"] == "unstable"
	assert transport.calls == []
	assert load_or_create_manifest(f)["status"] == "pending"


def test_upload_selected_uploads_a_settled_file(tmp_path: Path):
	root = tmp_path / "robot" / "2025-08-14"
	f = _pending_file(root, age_seconds=DEFAULT_STABILITY_SECONDS + 60)

	transport = RecordingTransport()
	q = UploadQueue(root=tmp_path / "robot", config=UploadAgentConfig(), transport=transport)
	res = q.upload_selected([f])

	assert res["uploaded"] == [str(f)]
	assert res["errors"] == []


def test_min_stable_age_seconds_zero_disables_the_gate(tmp_path: Path):
	"""The documented opt-out other unit tests use."""
	root = tmp_path / "robot" / "2025-08-14"
	f = _pending_file(root)  # mtime == now

	transport = RecordingTransport()
	q = UploadQueue(
		root=tmp_path / "robot",
		config=UploadAgentConfig(min_stable_age_seconds=0),
		transport=transport,
	)
	res = q.run_once()

	assert res["uploaded"] == str(f)
	assert res["skipped"] == []


def test_stability_window_defaults_to_phase01_value_and_is_configurable():
	from robot_vault_config.model import UploadAgentSettings, VaultConfig

	# Default is the real 5s window, not 0: a 0 default would make the gate
	# inert and leave the hole it exists to close.
	assert UploadAgentConfig().min_stable_age_seconds == DEFAULT_STABILITY_SECONDS

	vault = VaultConfig()
	vault.upload_agent = UploadAgentSettings(min_stable_age_seconds=12)
	assert UploadAgentConfig.from_vault(vault).min_stable_age_seconds == 12.0


def test_truncated_mcap_waits_for_its_footer_and_is_never_failed(tmp_path: Path):
	"""A buffered recorder can pause for longer than the mtime window. The
	file is then old by mtime but has no MCAP footer yet. It must stay
	pending (retried on a later scan) and must upload once the footer lands.
	"""
	root = tmp_path / "robot" / "2025-08-14"
	f = _pending_file(
		root, age_seconds=DEFAULT_STABILITY_SECONDS + 60, content=MCAP_MAGIC + b"partial"
	)

	transport = RecordingTransport()
	q = UploadQueue(
		root=tmp_path / "robot",
		config=UploadAgentConfig(min_stable_age_seconds=0),
		transport=transport,
	)
	first = q.run_many()
	assert first["uploaded"] == []
	assert first["skipped"] == [str(f)]
	assert first["errors"] == []
	assert transport.calls == []
	assert load_or_create_manifest(f)["status"] == "pending"

	f.write_bytes(COMPLETE_MCAP)
	second = q.run_many()
	assert second["uploaded"] == [str(f)]
	assert second["errors"] == []
