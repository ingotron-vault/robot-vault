"""Adversarial tests for the robot web filesystem boundary."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from robot_storage_manager.core import manifest_path_for
from robot_upload_agent.core import UploadAgentConfig, UploadQueue
from robot_vault_config.model import (
	DEFAULT_CHUNK_SIZE_BYTES,
	MAX_CHUNK_SIZE_BYTES,
	MAX_PARALLEL_STREAMS,
)
import robot_upload_agent.web as web


def _client(base: Path, monkeypatch, queue_factory=None) -> TestClient:
	monkeypatch.setattr(web, "BASE_ROOT", base)
	return TestClient(web.create_app(queue_factory=queue_factory))


def test_selected_upload_paths_cannot_escape_the_selected_root(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	sibling = base / "rover-2"
	outside = tmp_path / "outside"
	root.mkdir(parents=True)
	sibling.mkdir()
	outside.mkdir()

	sibling_file = sibling / "sibling.mcap"
	outside_file = outside / "secret.mcap"
	sibling_file.write_bytes(b"sibling")
	outside_file.write_bytes(b"secret")
	(root / "file-link.mcap").symlink_to(outside_file)
	(root / "dir-link").symlink_to(outside, target_is_directory=True)

	def queue_must_not_be_created(_root: Path):
		raise AssertionError("an invalid selection reached the upload queue")

	client = _client(base, monkeypatch, queue_must_not_be_created)
	attacks = [
		str(sibling_file),
		"../rover-2/sibling.mcap",
		str(root / "file-link.mcap"),
		str(root / "dir-link" / "secret.mcap"),
		str(outside_file),
	]
	for selected in attacks:
		response = client.post(
			"/api/upload/start",
			json={"root": str(root), "files": [selected]},
		)
		assert response.status_code == 400, (selected, response.text)

	assert not manifest_path_for(outside_file).exists()


class _RecordingQueue:
	def __init__(self, root: Path):
		self.root = root
		self.config = SimpleNamespace()
		self.calls = []

	def describe(self, files):
		self.calls.append(("describe", list(files)))
		return {"root": str(self.root), "pending": [str(path) for path in files]}

	def list_files(self, files):
		self.calls.append(("list_files", list(files)))
		return [
			{"path": str(path), "name": path.name, "size": 1, "mtime": 0, "status": "failed"}
			for path in files
		]

	def run_many(self, files=None):
		self.calls.append(("run_many", list(files or [])))
		return {"uploaded": [], "errors": [], "skipped": []}

	def upload_selected(self, paths, on_progress=None):
		self.calls.append(("upload_selected", list(paths)))
		return {"uploaded": [str(path) for path in paths], "errors": []}


def test_web_scans_pass_only_confined_candidates_to_queue_operations(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	outside = tmp_path / "outside"
	root.mkdir(parents=True)
	outside.mkdir()
	inside_file = root / "inside.mcap"
	outside_file = outside / "secret.mcap"
	inside_file.write_bytes(b"inside")
	outside_file.write_bytes(b"outside")
	(root / "leak.mcap").symlink_to(outside_file)

	queues = []

	def factory(queue_root: Path):
		queue = _RecordingQueue(queue_root)
		queues.append(queue)
		return queue

	client = _client(base, monkeypatch, factory)
	assert client.get("/api/status", params={"root": str(root)}).status_code == 200
	assert client.get("/api/files", params={"root": str(root)}).status_code == 200
	assert client.post("/api/upload/resume-all", json={"root": str(root)}).status_code == 200
	assert client.post("/api/upload/retry-failed", json={"root": str(root)}).status_code == 200

	for queue in queues:
		for _operation, paths in queue.calls:
			assert paths == [inside_file.resolve()]


def test_file_listing_does_not_create_a_manifest_for_an_outside_symlink_target(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	outside = tmp_path / "outside"
	root.mkdir(parents=True)
	outside.mkdir()
	inside_file = root / "inside.mcap"
	outside_file = outside / "secret.mcap"
	inside_file.write_bytes(b"inside")
	outside_file.write_bytes(b"outside")
	(root / "leak.mcap").symlink_to(outside_file)

	def factory(queue_root: Path) -> UploadQueue:
		return UploadQueue(
			root=queue_root,
			config=UploadAgentConfig(min_stable_age_seconds=0),
		)

	response = _client(base, monkeypatch, factory).get(
		"/api/files",
		params={"root": str(root)},
	)
	assert response.status_code == 200, response.text
	assert [item["path"] for item in response.json()] == [str(inside_file.resolve())]
	assert not manifest_path_for(outside_file).exists()


def test_upload_selection_is_canonicalized_and_deduplicated(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	root.mkdir(parents=True)
	real_file = root / "real.mcap"
	alias = root / "alias.mcap"
	real_file.write_bytes(b"data")
	alias.symlink_to(real_file)
	queues = []

	def factory(queue_root: Path):
		queue = _RecordingQueue(queue_root)
		queues.append(queue)
		return queue

	client = _client(base, monkeypatch, factory)
	response = client.post(
		"/api/upload/start",
		json={"root": str(root), "files": [str(alias), str(real_file)]},
	)
	assert response.status_code == 200, response.text
	job_id = response.json()["job_id"]

	for _ in range(100):
		job = client.get(f"/api/upload/jobs/{job_id}").json()
		if job["status"] == "completed":
			break
		time.sleep(0.01)

	assert [entry["path"] for entry in job["files"]] == [str(real_file.resolve())]
	upload_calls = [call for call in queues[0].calls if call[0] == "upload_selected"]
	assert upload_calls == [("upload_selected", [real_file.resolve()])]


@pytest.mark.parametrize(
	"options",
	[
		{"concurrency": 0},
		{"concurrency": "2"},
		{"bandwidth_mbps": -1},
		{"compute_chunk_hashes": "false"},
		{"chunk_size_bytes": 1},
		{"chunk_size_bytes": MAX_CHUNK_SIZE_BYTES + 1},
		{"unknown_option": 1},
	],
)
def test_upload_start_rejects_untyped_or_out_of_range_options(
	tmp_path: Path, monkeypatch, options
):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	root.mkdir(parents=True)
	data = root / "data.mcap"
	data.write_bytes(b"data")

	def queue_must_not_be_created(_root: Path):
		raise AssertionError("invalid options reached the upload queue")

	response = _client(base, monkeypatch, queue_must_not_be_created).post(
		"/api/upload/start",
		json={"root": str(root), "files": [str(data)], "options": options},
	)

	assert response.status_code == 422, response.text


def test_upload_start_applies_valid_typed_options(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	root.mkdir(parents=True)
	data = root / "data.mcap"
	data.write_bytes(b"data")
	queues = []

	def factory(queue_root: Path):
		queue = _RecordingQueue(queue_root)
		queues.append(queue)
		return queue

	options = {
		"concurrency": 3,
		"bandwidth_mbps": 50,
		"chunk_size_bytes": DEFAULT_CHUNK_SIZE_BYTES,
		"parallel_streams": MAX_PARALLEL_STREAMS,
		"compute_chunk_hashes": False,
	}
	response = _client(base, monkeypatch, factory).post(
		"/api/upload/start",
		json={"root": str(root), "files": [str(data)], "options": options},
	)

	assert response.status_code == 200, response.text
	assert vars(queues[0].config) == options


def test_exists_endpoint_is_not_an_outside_root_existence_oracle(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	outside = tmp_path / "outside"
	root.mkdir(parents=True)
	outside.mkdir()
	secret = outside / "secret.mcap"
	secret.write_bytes(b"secret")
	link = root / "secret-link.mcap"
	link.symlink_to(secret)
	client = _client(base, monkeypatch, lambda _root: None)

	for path in (str(secret), str(link), str(root / ".." / ".." / "outside" / "secret.mcap")):
		response = client.post("/api/exists", json={"paths": [path]})
		assert response.status_code == 400, (path, response.text)

	missing = root / "missing.mcap"
	response = client.post("/api/exists", json={"paths": [str(missing)]})
	assert response.status_code == 200
	assert response.json() == {"missing": [str(missing)]}


def test_subpath_cannot_switch_to_a_sibling_robot(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	(root).mkdir(parents=True)
	(base / "rover-2").mkdir()
	client = _client(base, monkeypatch, lambda _root: None)

	response = client.get(
		"/api/files",
		params={"root": str(root), "subpath": "../rover-2"},
	)
	assert response.status_code == 400, response.text


def test_history_skips_unconfined_data_paths_and_manifest_symlinks(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	root = base / "rover-1"
	outside = tmp_path / "outside"
	root.mkdir(parents=True)
	outside.mkdir()
	secret = outside / "secret.mcap"
	secret.write_bytes(b"secret")

	(root / "poison.mcap.manifest.json").write_text(json.dumps({
		"path": str(secret),
		"status": "completed",
	}))
	external_manifest = outside / "external.mcap.manifest.json"
	external_manifest.write_text(json.dumps({
		"path": str(secret),
		"status": "completed",
	}))
	(root / "linked.mcap.manifest.json").symlink_to(external_manifest)

	response = _client(base, monkeypatch, lambda _root: None).get(
		"/api/upload/history",
		params={"root": str(root)},
	)
	assert response.status_code == 200, response.text
	assert response.json()["files"] == []


def test_robot_discovery_skips_a_top_level_symlink_escape(tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	robot = base / "rover-1"
	outside = tmp_path / "outside-rover"
	robot.mkdir(parents=True)
	outside.mkdir()
	(robot / "inside.mcap").write_bytes(b"inside")
	outside_file = outside / "secret.mcap"
	outside_file.write_bytes(b"outside")
	(base / "escape").symlink_to(outside, target_is_directory=True)

	response = _client(base, monkeypatch, lambda _root: None).get("/api/robots/discover")
	assert response.status_code == 200, response.text
	assert [item["name"] for item in response.json()["robots"]] == ["rover-1"]
	assert not manifest_path_for(outside_file).exists()
