"""Coverage for the shutdown hook that un-strands in-flight uploads.

The agent runs as `python -m uvicorn robot_upload_agent.web:app`, so uvicorn is
PID 1 in the `robot` container and receives SIGTERM from `docker stop`. Its
graceful shutdown runs the ASGI lifespan shutdown and then exits, killing the
daemon upload threads without their except block ever restoring a manifest that
was set to 'uploading' before streaming. phase-01 recovers those on the next
scan after a 1h staleness window; this hook does it immediately.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from robot_storage_manager.core import load_or_create_manifest, save_manifest
from robot_upload_agent.web import JobStore, create_app, revert_inflight_uploads


def _file_with_status(tmp_path: Path, name: str, status: str) -> Path:
	f = tmp_path / name
	f.write_bytes(b"x" * 32)
	mf = load_or_create_manifest(f)
	mf["status"] = status
	save_manifest(mf)
	return f


def _status_of(f: Path) -> str:
	return (load_or_create_manifest(f).get("status") or "").lower()


def test_revert_resets_uploading_manifests_of_running_jobs(tmp_path: Path):
	f = _file_with_status(tmp_path, "a.mcap", "uploading")
	store = JobStore()
	store.create_job([str(f)])

	assert revert_inflight_uploads(store) == [str(f)]
	assert _status_of(f) == "failed"


def test_revert_leaves_finished_and_non_uploading_files_alone(tmp_path: Path):
	# Terminal manifest states are never touched...
	done = _file_with_status(tmp_path, "done.mcap", "completed")
	waiting = _file_with_status(tmp_path, "waiting.mcap", "pending")
	# ...nor is anything belonging to a job that already completed: its upload
	# call returned, so its manifests are already resolved.
	other_process = _file_with_status(tmp_path, "other.mcap", "uploading")

	store = JobStore()
	store.create_job([str(done), str(waiting)])
	finished = store.create_job([str(other_process)])
	store.mark_done(finished.job_id)

	assert revert_inflight_uploads(store) == []
	assert _status_of(done) == "completed"
	assert _status_of(waiting) == "pending"
	assert _status_of(other_process) == "uploading"


def test_revert_ignores_files_that_no_longer_exist(tmp_path: Path):
	"""Retention or an operator may have removed the file mid-job."""
	store = JobStore()
	store.create_job([str(tmp_path / "gone.mcap")])

	assert revert_inflight_uploads(store) == []


def test_lifespan_shutdown_reverts_in_flight_manifests(tmp_path: Path):
	"""End-to-end through the ASGI lifespan uvicorn drives on SIGTERM."""
	f = _file_with_status(tmp_path, "inflight.mcap", "uploading")
	app = create_app()

	with TestClient(app) as c:
		app.state.job_store.create_job([str(f)])
		assert c.get("/api/upload/jobs").status_code == 200
		# Still in flight while the app is up.
		assert _status_of(f) == "uploading"

	# Exiting the context runs the lifespan shutdown.
	assert _status_of(f) == "failed"


def test_lifespan_shutdown_cancels_active_queue_bandwidth_waits():
	app = create_app()
	class Queue:
		cancelled = False
		def cancel(self):
			self.cancelled = True
	queue = Queue()
	with TestClient(app):
		with app.state.active_upload_queues_lock:
			app.state.active_upload_queues["job"] = queue
	assert queue.cancelled is True
