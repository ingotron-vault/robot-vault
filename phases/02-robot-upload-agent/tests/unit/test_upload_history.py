"""Upload history derived from the on-disk manifests.

The robot SPA's History view does not read `/api/upload/jobs`, which is backed
by the in-memory `JobStore` and is therefore empty after any restart. A separate
history store would duplicate per-file status the JSON manifests already hold
and could add a second upload-status vocabulary (for example `uploaded`, which
the server never returns).

`/api/upload/history` derives the same facts from those manifests instead:
one source of truth, no new storage engine, no second vocabulary.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robot_vault_config.upload_status import UploadStatus
from robot_storage_manager.core import (
	DEFAULT_STALE_UPLOADING_SECONDS,
	load_or_create_manifest,
	manifest_path_for,
	read_manifest,
	save_manifest,
	scan_for_manifests,
)
import robot_upload_agent.web as web
from robot_upload_agent.web import create_app


@pytest.fixture(autouse=True)
def _confine_base_root_to_tmp_path(monkeypatch, tmp_path):
	# Root confinement checks every `root=`
	# against BASE_ROOT (/tmp/robot_data in production). This suite's tests
	# use tmp_path as their root, so BASE_ROOT is pointed at the same
	# tmp_path for the duration of each test rather than at the real
	# directory, which need not exist on the test host.
	monkeypatch.setattr(web, "BASE_ROOT", tmp_path)


def _file_with_manifest(root: Path, name: str, status: str, **extra) -> Path:
	f = root / name
	f.parent.mkdir(parents=True, exist_ok=True)
	f.write_bytes(b"x" * 64)
	mf = load_or_create_manifest(f)
	mf["status"] = status
	mf.update(extra)
	save_manifest(mf)
	return f


def _client() -> TestClient:
	return TestClient(create_app(queue_factory=lambda root: None))


def _history(tmp_path: Path, **params):
	resp = _client().get("/api/upload/history", params={"root": str(tmp_path), **params})
	assert resp.status_code == 200, resp.text
	return resp.json()


# --- the main property: it survives a restart -----------------------------

def test_history_survives_a_restart(tmp_path: Path):
	"""The main property: history survives a restart. Nothing is carried over in
	memory: the manifests are written, then a COMPLETELY fresh app (and thus
	a fresh, empty JobStore) is asked for the history."""
	_file_with_manifest(tmp_path, "a.mcap", UploadStatus.COMPLETED.value,
	                    upload_id="u-1", sha256_hash="abc")
	_file_with_manifest(tmp_path, "b.mcap", UploadStatus.FAILED.value)

	# A brand-new app object == a process restart as far as JobStore goes.
	client = _client()
	assert client.get("/api/upload/jobs").json() == [], (
		"the live view must be empty after a restart -- that is the premise"
	)

	body = client.get("/api/upload/history", params={"root": str(tmp_path)}).json()
	by_path = {Path(e["path"]).name: e for e in body["files"]}
	assert set(by_path) == {"a.mcap", "b.mcap"}
	assert by_path["a.mcap"]["status"] == UploadStatus.COMPLETED.value
	assert by_path["a.mcap"]["upload_id"] == "u-1"
	assert by_path["b.mcap"]["status"] == UploadStatus.FAILED.value


def test_empty_tree_is_an_empty_list_not_an_error(tmp_path: Path):
	assert _history(tmp_path)["files"] == []


def test_absent_root_is_an_empty_list_not_an_error(tmp_path: Path):
	body = _history(tmp_path / "never-created")
	assert body["files"] == []


# --- honesty about state --------------------------------------------------

def test_in_flight_upload_is_reported_as_uploading(tmp_path: Path):
	"""A fresh 'uploading' manifest belongs to a live upload and must be
	shown as such, not silently rounded to failed."""
	_file_with_manifest(tmp_path, "live.mcap", UploadStatus.UPLOADING.value)
	entry = _history(tmp_path)["files"][0]
	assert entry["status"] == UploadStatus.UPLOADING.value


def test_stale_uploading_manifest_is_reported_as_failed(tmp_path: Path):
	"""A manifest stranded at 'uploading' by a hard-killed process is what
	the next scan will reset to 'failed'. Reporting it as still uploading,
	forever, would be the dishonest option."""
	f = _file_with_manifest(tmp_path, "stranded.mcap", UploadStatus.UPLOADING.value)
	mf_path = manifest_path_for(f)
	# Age the manifest past the staleness window.
	future = mf_path.stat().st_mtime + DEFAULT_STALE_UPLOADING_SECONDS + 60
	manifest = read_manifest(mf_path, now=future)
	assert manifest["status"] == UploadStatus.FAILED.value


def test_history_reports_whether_the_data_file_still_exists(tmp_path: Path):
	"""Retention deletes the bytes; the manifest may outlive them. Saying the
	file is gone beats implying it is still on the robot."""
	f = _file_with_manifest(tmp_path, "gone.mcap", UploadStatus.COMPLETED.value)
	f.unlink()
	entry = _history(tmp_path)["files"][0]
	assert entry["data_file_present"] is False
	assert entry["status"] == UploadStatus.COMPLETED.value, (
		"deleting the bytes must not erase the record of the upload"
	)


def test_no_status_outside_the_canonical_vocabulary_is_ever_emitted(tmp_path: Path):
	"""Only canonical statuses are emitted. `cancelled`/`interrupted`
	are not values this system has."""
	_file_with_manifest(tmp_path, "a.mcap", UploadStatus.COMPLETED.value)
	_file_with_manifest(tmp_path, "c.mcap", "cancelled")  # not a real status
	_file_with_manifest(tmp_path, "d.mcap", UploadStatus.UPLOADING.value)

	legal = {s.value for s in UploadStatus}
	for entry in _history(tmp_path)["files"]:
		assert entry["status"] in legal, entry


# --- reading must not mutate the robot's data directory -------------------

def test_reading_history_never_writes_to_the_data_directory(tmp_path: Path):
	"""A GET that mutates state is a trap: `load_or_create_manifest` copies
	unparseable manifests to `.corrupt` and rewrites status on content
	drift, which is why the read path uses `read_manifest` instead."""
	f = _file_with_manifest(tmp_path, "a.mcap", UploadStatus.COMPLETED.value)
	mf_path = manifest_path_for(f)

	before = {p: p.stat().st_mtime_ns for p in sorted(tmp_path.rglob("*"))}
	before_text = mf_path.read_text()

	_history(tmp_path)

	after = {p: p.stat().st_mtime_ns for p in sorted(tmp_path.rglob("*"))}
	assert set(after) == set(before), "the listing created or removed a file"
	assert after == before, "the listing rewrote a file"
	assert mf_path.read_text() == before_text


def test_unparseable_manifest_is_skipped_not_fatal(tmp_path: Path):
	_file_with_manifest(tmp_path, "good.mcap", UploadStatus.COMPLETED.value)
	bad = tmp_path / "bad.mcap"
	bad.write_bytes(b"x")
	manifest_path_for(bad).write_text("{not json")

	body = _history(tmp_path)
	assert [Path(e["path"]).name for e in body["files"]] == ["good.mcap"]
	# ...and the corrupt file is left exactly as it was, with no `.corrupt`
	# copy written by a read-only request.
	assert not list(tmp_path.glob("*.corrupt"))


def test_content_drift_does_not_erase_a_completed_upload(tmp_path: Path):
	"""`load_or_create_manifest` resets status to 'pending' when the file's
	size/mtime drift, because the hash no longer describes the bytes. That is
	right for 'may I upload this?' and wrong for 'what happened to this
	file?' -- history must not forget a successful upload because someone
	touched the file afterwards."""
	f = _file_with_manifest(tmp_path, "a.mcap", UploadStatus.COMPLETED.value,
	                        upload_id="u-1")
	f.write_bytes(b"y" * 4096)  # size and mtime both change

	assert load_or_create_manifest(f)["status"] == UploadStatus.PENDING.value
	entry = _history(tmp_path)["files"][0]
	assert entry["status"] == UploadStatus.COMPLETED.value
	assert entry["upload_id"] == "u-1"


# --- the storage-layer helpers ---------------------------------------------

def test_scan_for_manifests_ignores_corrupt_copies(tmp_path: Path):
	f = _file_with_manifest(tmp_path, "a.mcap", UploadStatus.COMPLETED.value)
	corrupt = manifest_path_for(f)
	corrupt.with_name(corrupt.name + ".corrupt").write_text("{}")

	found = scan_for_manifests(tmp_path)
	assert found == [manifest_path_for(f).resolve()]


def test_scan_for_manifests_recurses(tmp_path: Path):
	_file_with_manifest(tmp_path, "rover-1/day1/a.mcap", UploadStatus.COMPLETED.value)
	_file_with_manifest(tmp_path, "rover-2/b.mcap", UploadStatus.FAILED.value)
	assert len(scan_for_manifests(tmp_path)) == 2


def test_read_manifest_returns_none_for_garbage(tmp_path: Path):
	p = tmp_path / "x.manifest.json"
	p.write_text("[]")  # valid JSON, wrong shape
	assert read_manifest(p) is None
	assert read_manifest(tmp_path / "missing.manifest.json") is None


def test_history_is_newest_first(tmp_path: Path):
	_file_with_manifest(tmp_path, "old.mcap", UploadStatus.COMPLETED.value,
	                    recorded_at="2023-01-01T00:00:00+00:00")
	_file_with_manifest(tmp_path, "new.mcap", UploadStatus.COMPLETED.value,
	                    recorded_at="2024-06-01T00:00:00+00:00")
	names = [Path(e["path"]).name for e in _history(tmp_path)["files"]]
	assert names == ["new.mcap", "old.mcap"]


def test_ordering_is_chronological_not_lexical(tmp_path: Path):
	"""Same wall-clock text, different UTC offsets: `+05:00` is the EARLIER
	instant but sorts later as a string. A lexical compare gets this
	backwards."""
	_file_with_manifest(tmp_path, "earlier.mcap", UploadStatus.COMPLETED.value,
	                    recorded_at="2024-06-01T12:00:00+05:00")
	_file_with_manifest(tmp_path, "later.mcap", UploadStatus.COMPLETED.value,
	                    recorded_at="2024-06-01T12:00:00+00:00")
	names = [Path(e["path"]).name for e in _history(tmp_path)["files"]]
	assert names == ["later.mcap", "earlier.mcap"]


def test_z_suffixed_timestamps_are_understood(tmp_path: Path):
	_file_with_manifest(tmp_path, "old.mcap", UploadStatus.COMPLETED.value,
	                    recorded_at="2023-01-01T00:00:00Z")
	_file_with_manifest(tmp_path, "new.mcap", UploadStatus.COMPLETED.value,
	                    recorded_at="2024-06-01T00:00:00Z")
	names = [Path(e["path"]).name for e in _history(tmp_path)["files"]]
	assert names == ["new.mcap", "old.mcap"]


def test_an_unparseable_timestamp_sorts_last_not_first(tmp_path: Path):
	"""'newest first' is a claim we cannot make about a row whose stamp is
	not a time, so it must not be floated to the top."""
	_file_with_manifest(tmp_path, "real.mcap", UploadStatus.COMPLETED.value,
	                    recorded_at="2020-01-01T00:00:00+00:00")
	junk = tmp_path / "junk.mcap"
	junk.write_bytes(b"x")
	manifest_path_for(junk).write_text(json.dumps({
		"path": str(junk.resolve()),
		"status": UploadStatus.COMPLETED.value,
		"recorded_at": "whenever",
	}))

	names = [Path(e["path"]).name for e in _history(tmp_path)["files"]]
	assert names == ["real.mcap", "junk.mcap"]


def test_one_odd_timestamp_does_not_take_down_the_whole_listing(tmp_path: Path):
	"""A manifest whose `created_at` is a number rather than an ISO string --
	hand-edited, or written by another tool -- must not raise `TypeError` in
	the sort and 500 the entire endpoint. One bad record must not hide every
	good one."""
	_file_with_manifest(tmp_path, "good.mcap", UploadStatus.COMPLETED.value,
	                    created_at="2024-01-01T00:00:00+00:00")
	odd = tmp_path / "odd.mcap"
	odd.write_bytes(b"x")
	manifest_path_for(odd).write_text(json.dumps({
		"path": str(odd.resolve()),
		"status": UploadStatus.COMPLETED.value,
		"created_at": 1_700_000_000,   # a number, not a string
	}))

	names = {Path(e["path"]).name for e in _history(tmp_path)["files"]}
	assert names == {"good.mcap", "odd.mcap"}


def test_a_non_string_path_does_not_take_down_the_whole_listing(tmp_path: Path):
	"""`Path(12345)` raises TypeError. One foreign or hand-edited manifest
	must be skipped, not 500 the endpoint and hide every good record."""
	_file_with_manifest(tmp_path, "good.mcap", UploadStatus.COMPLETED.value)
	(tmp_path / "bad.mcap.manifest.json").write_text(
		json.dumps({"path": 12345, "status": UploadStatus.COMPLETED.value})
	)

	names = [Path(e["path"]).name for e in _history(tmp_path)["files"]]
	assert names == ["good.mcap"]


def test_an_unusable_manifest_path_is_skipped_rather_than_raising(tmp_path: Path):
	"""An unusable/unconfined manifest path must not become a filesystem
	existence oracle, and one bad record must not 500 the listing."""
	_file_with_manifest(tmp_path, "good.mcap", UploadStatus.COMPLETED.value)
	(tmp_path / "weird.mcap.manifest.json").write_text(
		json.dumps({
			"path": "/" + "x" * 5000 + "/nope.mcap",
			"status": UploadStatus.COMPLETED.value,
		})
	)

	entries = {Path(e["path"]).name: e for e in _history(tmp_path)["files"]}
	assert set(entries) == {"good.mcap"}


def test_a_directory_symlink_does_not_duplicate_history_rows(tmp_path: Path):
	"""`scan_for_manifests` resolves paths, so a symlinked directory inside
	the tree would otherwise yield every manifest under it twice."""
	real = tmp_path / "real"
	_file_with_manifest(real, "a.mcap", UploadStatus.COMPLETED.value)
	(tmp_path / "alias").symlink_to(real, target_is_directory=True)

	paths = [e["path"] for e in _history(tmp_path)["files"]]
	assert len(paths) == len(set(paths)), f"duplicate rows: {paths}"


def test_limit_is_reported_rather_than_silently_truncating(tmp_path: Path):
	for i in range(5):
		_file_with_manifest(tmp_path, f"f{i}.mcap", UploadStatus.COMPLETED.value)
	body = _history(tmp_path, limit=2)
	assert len(body["files"]) == 2
	assert body["truncated"] is True
	assert _history(tmp_path, limit=100)["truncated"] is False
