from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from robot_storage_manager.core import (
	MANIFEST_SUFFIX,
	ConcurrentManifestUpdateError,
	_same_manifest_content_strict,
	_stat_identity,
	load_or_create_manifest,
	manifest_path_for,
	read_manifest,
	save_manifest,
)
from robot_vault_config.model import (
	MAX_CHUNK_SIZE_BYTES,
	MAX_PARALLEL_STREAMS,
	MIN_S3_PART_BYTES,
)
from robot_vault_config.upload_status import UploadStatus, parse as parse_status

from .core import UploadQueue, recording_evidence

BASE_ROOT = Path("/tmp/robot_data")

# Where vite.config.ts's `build.outDir` puts the SPA build (see the mount in
# create_app below). Module-level so tests can monkeypatch it to point at a
# tmp_path instead of the real build output.
UI_DIR = Path(__file__).parent / "ui"

logger = logging.getLogger(__name__)


class UploadRuntimeOptions(BaseModel):
	"""Strict request-scoped upload settings accepted by the browser API."""

	model_config = ConfigDict(extra="forbid", strict=True)

	concurrency: Optional[StrictInt] = Field(default=None, ge=1, le=32)
	bandwidth_mbps: Optional[StrictInt] = Field(
		default=None, ge=0, le=1_000_000
	)
	chunk_size_bytes: Optional[StrictInt] = Field(
		default=None, ge=MIN_S3_PART_BYTES, le=MAX_CHUNK_SIZE_BYTES
	)
	parallel_streams: Optional[StrictInt] = Field(
		default=None, ge=1, le=MAX_PARALLEL_STREAMS
	)
	compute_chunk_hashes: Optional[StrictBool] = None

class UploadStartRequest(BaseModel):
	model_config = ConfigDict(extra="forbid", strict=True)

	root: str = Field(min_length=1)
	files: List[str] = Field(min_length=1, max_length=10_000)
	options: UploadRuntimeOptions = Field(default_factory=UploadRuntimeOptions)


# Resolve Vault config path for server connection (ingest host/port, TLS, etc.)
def _resolve_vault_config_path() -> Optional[Path]:
    # Priority: explicit env var -> local.json -> default json -> None
    env_path = os.getenv("VAULT_CONFIG_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    for candidate in [
        Path("/app/configs/vault.local.json"),
        Path("/app/configs/vault.json"),
    ]:
        if candidate.exists():
            return candidate
    return None


def _resolve_confined_path(
	value: Any,
	*,
	within: Optional[Path] = None,
	field: str = "path",
) -> Path:
	"""Return one canonical path, confined to a canonical directory boundary.

	All request-controlled filesystem paths go through this helper. Relative
	values are interpreted below ``within`` (or ``BASE_ROOT``), while absolute
	values are allowed only when they resolve to that same directory or one of
	its descendants. ``resolve(strict=False)`` collapses ``..`` and follows
	symlinks even when the final leaf does not exist, so a symlinked parent cannot
	be used to escape the boundary.

	The returned path no longer contains the attacker-controlled symlink or
	traversal components. Callers must pass this returned value onward rather
	than reusing the raw request value.
	"""
	if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
		raise HTTPException(400, detail=f"{field} must be a non-empty filesystem path")

	try:
		boundary = Path(BASE_ROOT if within is None else within).resolve(strict=False)
		candidate = Path(value)
		if not candidate.is_absolute():
			candidate = boundary / candidate
		resolved = candidate.resolve(strict=False)
	except (OSError, RuntimeError, TypeError, ValueError):
		raise HTTPException(400, detail=f"{field} is not a usable filesystem path") from None

	if not resolved.is_relative_to(boundary):
		raise HTTPException(
			400,
			detail=f"{field} must resolve to {boundary} or a path inside it",
		)
	return resolved


def _confined_files(root: Path, suffix: str) -> List[Path]:
	"""Walk regular files below ``root`` without following directory symlinks.

	``Path.rglob``/the storage scanner resolve file symlinks before returning
	them. That is convenient for CLI scans but unsafe for a web boundary: an
	attacker who can plant ``inside.mcap -> /outside/secret`` could make a GET
	read or create a manifest beside the outside target. This walker resolves
	and confines every candidate *before* checking it as a regular file.
	"""
	root = _resolve_confined_path(root, field="root")
	seen: Dict[Path, None] = {}
	try:
		for current, directories, filenames in os.walk(root, followlinks=False):
			current_path = _resolve_confined_path(current, within=root)
			# Be explicit rather than relying only on os.walk's followlinks=False:
			# a directory symlink is never part of the web-visible tree, even when
			# it happens to point back inside the root.
			directories[:] = [
				name
				for name in directories
				if not (current_path / name).is_symlink()
			]
			for name in filenames:
				if not name.endswith(suffix):
					continue
				try:
					candidate = _resolve_confined_path(current_path / name, within=root)
				except HTTPException:
					logger.warning("Skipping file that resolves outside %s: %s", root, current_path / name)
					continue
				try:
					if candidate.is_file():
						seen.setdefault(candidate, None)
				except OSError:
					logger.warning("Skipping unreadable file %s", candidate, exc_info=True)
	except OSError:
		logger.warning("Could not scan confined root %s", root, exc_info=True)
	return sorted(seen)


@dataclass
class JobFileState:
	path: str
	bytes_sent: int
	total_bytes: int
	# One of the canonical UploadStatus values: pending |
	# uploading | completed | failed. Serialized straight to the robot UI.
	status: str
	error: Optional[Dict[str, Any]] = None


@dataclass
class JobState:
	job_id: str
	started_at: float
	completed_at: Optional[float]
	files: Dict[str, JobFileState]
	status: str  # running|completed


class JobStore:
	def __init__(self) -> None:
		self._jobs: Dict[str, JobState] = {}
		self._lock = threading.Lock()

	def create_job(self, file_paths: List[str]) -> JobState:
		with self._lock:
			jid = str(uuid.uuid4())
			files = {
				p: JobFileState(
					path=p, bytes_sent=0, total_bytes=0, status=UploadStatus.PENDING.value
				)
				for p in file_paths
			}
			job = JobState(job_id=jid, started_at=time.time(), completed_at=None, files=files, status="running")
			self._jobs[jid] = job
			return job

	def get(self, job_id: str) -> Optional[JobState]:
		with self._lock:
			return self._jobs.get(job_id)

	def list_jobs(self) -> List[JobState]:
		with self._lock:
			# Return newest first
			return sorted(list(self._jobs.values()), key=lambda j: j.started_at, reverse=True)

	def update_progress(self, job_id: str, path: str, bytes_sent: int, total: int) -> None:
		with self._lock:
			job = self._jobs.get(job_id)
			if not job:
				return
			fs = job.files.get(path)
			if not fs:
				fs = JobFileState(
					path=path,
					bytes_sent=0,
					total_bytes=total,
					status=UploadStatus.UPLOADING.value,
				)
				job.files[path] = fs
			fs.bytes_sent = bytes_sent
			fs.total_bytes = total
			if fs.status == UploadStatus.PENDING:
				fs.status = UploadStatus.UPLOADING.value

	def mark_file_done(self, job_id: str, path: str) -> None:
		with self._lock:
			job = self._jobs.get(job_id)
			if not job:
				return
			fs = job.files.get(path)
			if fs:
				fs.status = UploadStatus.COMPLETED.value

	def mark_file_failed(self, job_id: str, path: str, error: Dict[str, Any]) -> None:
		with self._lock:
			job = self._jobs.get(job_id)
			if not job:
				return
			fs = job.files.get(path)
			if fs:
				fs.status = UploadStatus.FAILED.value
				fs.error = error

	def mark_done(self, job_id: str) -> None:
		with self._lock:
			job = self._jobs.get(job_id)
			if job:
				# Every selected file needs an outcome when its attempt ends.
				for fs in job.files.values():
					if parse_status(fs.status) not in (UploadStatus.COMPLETED, UploadStatus.FAILED):
						fs.status = UploadStatus.FAILED.value
						fs.error = {"status": "no_result", "detail": "This upload attempt ended without a file outcome. Retry the file."}
				job.status = "completed"
				job.completed_at = time.time()

	def inflight_paths(self) -> List[str]:
		"""Files of still-running jobs that haven't reached a terminal state.

		Snapshotted under the lock: the upload threads mutate job.files while
		this runs. This is the only in-memory record of what this process has
		in flight (each request builds a fresh UploadQueue, so there is no
		queue registry to consult).
		"""
		with self._lock:
			return [
				fs.path
				for job in self._jobs.values()
				if job.status == "running"
				for fs in job.files.values()
				if parse_status(fs.status)
				not in (UploadStatus.COMPLETED, UploadStatus.FAILED)
			]


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _history_sort_key(entry: Dict[str, Any]) -> datetime:
	"""Absolute time for one history row, for newest-first ordering.

	Returns the epoch for anything unparseable or missing, which sorts such
	rows LAST under `reverse=True`. Never raises: a manifest whose stamp is a
	number, a list, or free text must not 500 a listing.
	"""
	raw = entry.get("recorded_at") or entry.get("created_at")
	if not isinstance(raw, str):
		return _EPOCH
	text = raw.strip()
	if text.endswith("Z"):
		text = text[:-1] + "+00:00"
	try:
		parsed = datetime.fromisoformat(text)
	except ValueError:
		return _EPOCH
	# A naive stamp is UTC by the system-wide timestamp convention, and mixing
	# naive with aware would raise on comparison.
	return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def revert_inflight_uploads(store: JobStore) -> List[str]:
	"""Reset manifests this process left at status=='uploading' back to 'failed'.

	The upload path sets status='uploading' before streaming and restores it in
	an except block, but SIGTERM (`docker stop`, which uvicorn turns into a
	graceful shutdown) tears the process down without running that block, so
	the manifest stays 'uploading' -- a status the scan loops never re-select.
	robot_storage_manager reconciles those on the next scan once they're older
	than DEFAULT_STALE_UPLOADING_SECONDS (1h); doing it here makes the same
	recovery immediate instead.

	This is deliberately best-effort and never blocks shutdown:
	  - Only files of jobs that are still running are considered -- the ones
	    this process can actually have in flight.
	  - It does not join the (daemon) upload threads. If one of them finishes
	    successfully in the window before the process exits it just rewrites
	    'completed' over our 'failed', which is the correct outcome; save_manifest
	    is atomic, so the two writers can't tear a manifest.
	  - Conversely a thread that reaches its *next* file after this sweep can
	    still strand an 'uploading' manifest. That case is rare and remains
	    covered by the 1h reconcile, which this handler complements rather than
	    replaces.

	Returns the paths that were reverted (for logging/tests).
	"""
	reverted: List[str] = []
	for path in store.inflight_paths():
		try:
			p = Path(path)
			if not p.exists():
				continue
			mf = load_or_create_manifest(p)
			if parse_status(mf.get("status")) is not UploadStatus.UPLOADING:
				continue
			mf["status"] = UploadStatus.FAILED.value
			mf["last_upload_error"] = {"status": "interrupted", "detail": "The upload agent stopped before this upload finished."}
			mf["upload_updated_at"] = datetime.now(timezone.utc).isoformat()
			save_manifest(mf)
			reverted.append(str(p))
		except Exception:
			# A shutdown hook that raises would mask the real shutdown; the 1h
			# reconcile still covers anything we fail to revert here.
			logger.exception("Failed to revert in-flight manifest for %s", path)
	return reverted


def create_app(queue_factory: Optional[Callable[[Path], UploadQueue]] = None) -> FastAPI:
	@asynccontextmanager
	async def lifespan(app: FastAPI):
		yield
		# Shutdown. uvicorn (PID 1 in the `robot` container) runs this on
		# SIGTERM before exiting, which is what kills the daemon upload
		# threads -- so this is the last chance to un-strand their manifests.
		with app.state.active_upload_queues_lock:
			active_queues = list(app.state.active_upload_queues.values())
		for queue in active_queues:
			cancel = getattr(queue, "cancel", None)
			if callable(cancel):
				cancel()
		store: Optional[JobStore] = getattr(app.state, "job_store", None)
		if store is None:
			return
		reverted = revert_inflight_uploads(store)
		if reverted:
			logger.warning(
				"Shutdown: reset %d in-flight upload manifest(s) to 'failed' so they "
				"are re-uploaded on the next scan: %s",
				len(reverted),
				", ".join(reverted),
			)

	app = FastAPI(title="Robot Upload UI", lifespan=lifespan)
	app.state.job_store = JobStore()
	app.state.active_upload_queues = {}
	app.state.active_upload_queues_lock = threading.Lock()

	def _queue_for_root(root: Path) -> UploadQueue:
		if queue_factory:
			return queue_factory(root)
		cfg_path = _resolve_vault_config_path()
		return UploadQueue(root=root, vault_config_path=cfg_path)

	# --- SPA (Vite build) mount -------------------------------------
	# vite.config.ts builds straight into this directory (outDir:
	# "../robot_upload_agent/ui") specifically so this mount can serve
	# it. There is deliberately no hand-written fallback UI: it would render
	# manifest/request data through innerHTML and add a second, unaudited UI
	# surface.
	#
	# The SPA is hash-routed (ui-spa/src/lib/hashRoute.ts: "#/history"
	# etc.), so the browser only ever requests the literal path "/" for
	# the app shell -- no catch-all "unknown path -> index.html"
	# fallback is needed, just "/" plus the built /assets/*.
	spa_dir = UI_DIR
	spa_index = spa_dir / "index.html"
	if spa_index.is_file():
		# Bundle filenames are content-hashed by Vite (index-XXXXXXXX.js),
		# so they're safe to cache forever; only index.html itself (whose
		# name never changes) needs no-cache, so a rebuilt SPA's hashed asset
		# references are always picked up by clients with a previously cached shell.
		app.mount("/assets", StaticFiles(directory=spa_dir / "assets"), name="spa-assets")
		_spa_index_html = spa_index.read_text()

		@app.get("/", response_class=HTMLResponse)
		def spa_index_route() -> HTMLResponse:
			response = HTMLResponse(_spa_index_html)
			response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
			return response
	else:
		# A source checkout does not contain generated Vite artifacts. Do not
		# add a hand-written fallback UI here: it would interpolate filesystem and
		# manifest values through innerHTML and be a stored-XSS surface. The API
		# remains available, while the browser gets an explicit build error.
		logger.error(
			"SPA build not found at %s; run `make ui-build` before starting the robot UI",
			spa_dir,
		)
		_missing_spa_html = (
			"<!doctype html><html><head><meta charset=\"utf-8\">"
			"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
			"<title>Robot Upload UI unavailable</title></head><body>"
			"<h1>Robot Upload UI is not built</h1>"
			"<p>Run <code>make ui-build</code>, then restart the robot service.</p>"
			"</body></html>"
		)

		@app.get("/", response_class=HTMLResponse)
		def missing_spa_index_route() -> HTMLResponse:
			return HTMLResponse(
				_missing_spa_html,
				status_code=503,
				headers={
					"Cache-Control": "no-store",
					"Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
				},
			)

	@app.get("/api/status")
	def api_status(root: str = Query(...)):
		root_path = _resolve_confined_path(root, field="root")
		q = _queue_for_root(root_path)
		return q.describe(files=_confined_files(root_path, ".mcap"))

	@app.get("/api/files")
	def api_files(root: str = Query(...), subpath: Optional[str] = None):
		base = _resolve_confined_path(root, field="root")
		# A subpath is confined to the selected root, not merely to a sibling
		# somewhere else below BASE_ROOT.
		scan_root = base if subpath is None else _resolve_confined_path(
			subpath,
			within=base,
			field="subpath",
		)
		q = _queue_for_root(base)
		return q.list_files(files=_confined_files(scan_root, ".mcap"))

	@app.post("/api/upload/start")
	def api_upload_start(payload: UploadStartRequest):
		root = payload.root
		files = payload.files
		options = payload.options
		root_path = _resolve_confined_path(root, field="root")
		selected_files: List[Path] = []
		seen: Dict[Path, None] = {}
		for index, raw_path in enumerate(files):
			path = _resolve_confined_path(
				raw_path,
				within=root_path,
				field=f"files[{index}]",
			)
			if path.suffix.lower() != ".mcap":
				raise HTTPException(400, detail=f"files[{index}] must be an .mcap file")
			try:
				is_file = path.is_file()
			except OSError:
				is_file = False
			if not is_file:
				raise HTTPException(400, detail=f"files[{index}] does not exist as a regular file")
			if path not in seen:
				seen[path] = None
				selected_files.append(path)
		q = _queue_for_root(root_path)
		# Apply runtime options if provided
		if options.concurrency is not None:
			q.config.concurrency = options.concurrency
		if options.bandwidth_mbps is not None:
			set_bandwidth = getattr(q, "set_bandwidth_mbps", None)
			if callable(set_bandwidth):
				set_bandwidth(options.bandwidth_mbps)
			else:  # compatible with injected queue implementations
				q.config.bandwidth_mbps = options.bandwidth_mbps
		if options.chunk_size_bytes is not None:
			q.config.chunk_size_bytes = options.chunk_size_bytes
		if options.parallel_streams is not None:
			q.config.parallel_streams = options.parallel_streams
		if options.compute_chunk_hashes is not None:
			q.config.compute_chunk_hashes = options.compute_chunk_hashes
		store: JobStore = app.state.job_store
		job = store.create_job([str(p) for p in selected_files])
		with app.state.active_upload_queues_lock:
			app.state.active_upload_queues[job.job_id] = q

		def _run():
			job_error: Optional[Dict[str, Any]] = None
			def on_prog(path: str, sent: int, total: int) -> None:
				store.update_progress(job.job_id, path, sent, total)
			try:
				# Check which files are already uploaded before attempting upload
				files_to_upload = []
				for fp in selected_files:
					try:
						mf = load_or_create_manifest(fp)
					except (OSError, ValueError, ConcurrentManifestUpdateError) as exc:
						store.mark_file_failed(job.job_id, str(fp), {"status": "preparation_failed", "detail": str(exc)})
						continue
					if parse_status(mf.get("status")) is UploadStatus.COMPLETED:
						store.mark_file_done(job.job_id, str(fp))
					else:
						files_to_upload.append(fp)
				
				# Only upload files that aren't already uploaded
				if files_to_upload:
					res = q.upload_selected(files_to_upload, on_progress=on_prog)
					# Update outcomes
					for p in res.get("uploaded", []):
						store.mark_file_done(job.job_id, str(Path(p)))
					for e in res.get("errors", []):
						store.mark_file_failed(job.job_id, e.get("file", ""), e.get("error") or {})
				
			except Exception as exc:
				job_error = {"status": "exception", "detail": str(exc)}
			finally:
				try:
					# Queues skip recordings already in flight. Read their outcome
					# without changing another job's manifest. Durable completion
					# also survives an unexpected error later in the same queue.
					snapshot = store.get(job.job_id)
					for path in selected_files:
						state = snapshot.files.get(str(path)) if snapshot is not None else None
						if not state or parse_status(state.status) in (UploadStatus.COMPLETED, UploadStatus.FAILED):
							continue
						manifest = read_manifest(manifest_path_for(path)) or {}
						status = parse_status(manifest.get("status"))
						if status is UploadStatus.COMPLETED:
							try:
								identity = _stat_identity(path.stat())
								exact_recording = (
									manifest.get("path") == str(path)
									and _same_manifest_content_strict(manifest, identity)
								)
							except OSError:
								exact_recording = False
							if exact_recording:
								store.mark_file_done(job.job_id, str(path))
							else:
								store.mark_file_failed(job.job_id, str(path), {
									"status": "recording_changed",
									"detail": "This recording changed or disappeared after upload. Retry the current file.",
								})
						elif status is UploadStatus.UPLOADING:
							store.mark_file_failed(job.job_id, str(path), {
								"status": "in_progress",
								"detail": "An upload of this file is still in progress. Retry after it finishes.",
							})
						else:
							store.mark_file_failed(job.job_id, str(path), job_error or {
								"status": "no_result",
								"detail": "This upload attempt ended without a file outcome. Retry the file.",
							})
				finally:
					store.mark_done(job.job_id)
					with app.state.active_upload_queues_lock:
						app.state.active_upload_queues.pop(job.job_id, None)

		threading.Thread(target=_run, daemon=True).start()
		return {"job_id": job.job_id}

	@app.get("/api/upload/jobs")
	def api_jobs():
		store: JobStore = app.state.job_store
		jobs = store.list_jobs()
		return [
			{
				"job_id": j.job_id,
				"status": j.status,
				"started_at": j.started_at,
				"completed_at": j.completed_at,
				"files": [
					{
						"path": fs.path,
						"bytes_sent": fs.bytes_sent,
						"total_bytes": fs.total_bytes,
						"status": fs.status,
						"error": fs.error,
					}
					for fs in j.files.values()
				],
			}
			for j in jobs
		]

	@app.get("/api/upload/jobs/{job_id}")
	def api_job(job_id: str):
		store: JobStore = app.state.job_store
		job = store.get(job_id)
		if not job:
			raise HTTPException(404, detail="job not found")
		return {
			"job_id": job.job_id,
			"status": job.status,
			"started_at": job.started_at,
			"completed_at": job.completed_at,
			"files": [
				{
					"path": fs.path,
					"bytes_sent": fs.bytes_sent,
					"total_bytes": fs.total_bytes,
					"status": fs.status,
					"error": fs.error,
				}
				for fs in job.files.values()
			],
		}

	@app.get("/api/upload/history")
	def api_upload_history(
		root: Optional[str] = Query(None),
		limit: int = Query(500, ge=1, le=5000),
	):
		"""Per-file upload history, derived from the on-disk manifests.

		`/api/upload/jobs` above is the LIVE view: it reads
		`JobStore`, which is in-memory and therefore empty after a restart.
		This endpoint is the DURABLE view, reconstructed from the manifests
		the storage layer already maintains. The split is deliberate and the
		two cannot disagree, because they answer different questions:

		  /api/upload/jobs     what THIS process is doing right now, with
		                       live byte counters and job grouping
		  /api/upload/history  what has happened to each file, surviving any
		                       number of restarts, with no byte counters

		Why per-FILE and not per-job: manifests are per-file sidecars and
		carry no job identifier, so there is nothing to reconstruct a job from.
		The alternative -- persisting a job->files grouping alongside them --
		would be a second store of the same facts that can drift apart. A flat
		log is what the data actually supports.

		Status is the canonical `UploadStatus` read straight out of the
		manifest. No status is invented here: there is
		no 'cancelled' or 'interrupted', because nothing writes those.
		"""
		root_path = _resolve_confined_path(root if root else BASE_ROOT, field="root")
		if not root_path.exists():
			# An empty/absent tree is an empty history, not an error -- a
			# fresh robot has uploaded nothing yet.
			return {"root": str(root_path), "files": [], "truncated": False}

		entries: List[Dict[str, Any]] = []
		for manifest_path in _confined_files(root_path, MANIFEST_SUFFIX):
			manifest = read_manifest(manifest_path)
			if manifest is None:
				continue
			path = manifest.get("path")
			# Must be a string: `Path(12345)` raises TypeError, and one
			# hand-edited or foreign manifest would then 500 the whole
			# listing instead of being skipped. Same failure shape as the
			# sort key below.
			if not path or not isinstance(path, str):
				continue
			try:
				data_path = _resolve_confined_path(path, within=root_path, field="manifest path")
			except HTTPException:
				logger.warning(
					"Skipping manifest %s whose data path is outside %s",
					manifest_path,
					root_path,
				)
				continue
			manifest_status: UploadStatus = (
				parse_status(manifest.get("status"), UploadStatus.PENDING)
				or UploadStatus.PENDING
			)
			entries.append(
				{
					"path": str(data_path),
					"status": manifest_status.value,
					"file_size_bytes": manifest.get("file_size_bytes"),
					"sha256_hash": manifest.get("sha256_hash"),
					"upload_id": manifest.get("upload_id"),
					"recorded_at": manifest.get("recorded_at"),
					"created_at": manifest.get("created_at"),
					"labels": manifest.get("labels"),
					# The data file may be gone (retention) while the manifest
					# remains; say so rather than implying the bytes are there.
					# `is_file()` swallows OSError itself, so an unreadable
					# path reports False rather than failing the listing.
					**recording_evidence(data_path, manifest),
				}
			)

		# Newest first, by absolute time.
		#
		# Deliberately parsed rather than compared as strings. Lexical order
		# equals chronological order only while every stamp is UTC with the
		# same format -- which is true of what this agent writes
		# (`datetime.now(timezone.utc).isoformat()`) and not guaranteed of a
		# manifest written by anything else: "…T00:00:00+05:00" sorts after
		# "…T00:00:00+00:00" while being the earlier instant, and a stamp
		# that is not a timestamp at all ("unknown") sorts above every real
		# one. Unparseable and missing stamps go LAST rather than first,
		# since "newest" is a claim we cannot make about them.
		entries.sort(key=_history_sort_key, reverse=True)
		truncated = len(entries) > limit
		return {
			"root": str(root_path),
			"files": entries[:limit],
			"truncated": truncated,
		}

	@app.post("/api/upload/resume-all")
	def api_resume_all(payload: Dict[str, Any]):
		root = payload.get("root")
		if not root:
			raise HTTPException(400, detail="root is required")
		root_path = _resolve_confined_path(root, field="root")
		q = _queue_for_root(root_path)
		res = q.run_many(files=_confined_files(root_path, ".mcap"))
		return res

	@app.post("/api/upload/retry-failed")
	def api_retry_failed(payload: Dict[str, Any]):
		root = payload.get("root")
		if not root:
			raise HTTPException(400, detail="root is required")
		root_path = _resolve_confined_path(root, field="root")
		q = _queue_for_root(root_path)
		failed: List[Path] = []
		for item in q.list_files(files=_confined_files(root_path, ".mcap")):
			if parse_status(item.get("status")) is not UploadStatus.FAILED:
				continue
			try:
				failed.append(
					_resolve_confined_path(item.get("path"), within=root_path, field="retry path")
				)
			except HTTPException:
				logger.warning("Skipping unconfined retry candidate: %r", item.get("path"))
		res = q.upload_selected(failed)
		return res

	@app.post("/api/exists")
	def api_exists(payload: Dict[str, Any]):
		paths = payload.get("paths") or []
		if not isinstance(paths, list):
			raise HTTPException(400, detail="paths must be a list")
		missing: List[str] = []
		for index, p in enumerate(paths):
			pp = _resolve_confined_path(p, field=f"paths[{index}]")
			try:
				if not pp.exists() or not pp.is_file():
					missing.append(p)
			except (OSError, ValueError):
				missing.append(p)
		return {"missing": missing}

	@app.get("/api/robots/discover")
	def api_discover_robots():
		"""Discover robot directories without following links out of BASE_ROOT."""
		try:
			robots: List[Dict[str, Any]] = []
			base_path = _resolve_confined_path(BASE_ROOT, field="root")
			seen_roots: Dict[Path, None] = {}
			if base_path.exists():
				for item in base_path.iterdir():
					if item.name.startswith("."):
						continue
					try:
						robot_root = _resolve_confined_path(item, within=base_path, field="robot path")
					except HTTPException:
						logger.warning("Skipping robot directory that escapes %s: %s", base_path, item)
						continue
					if robot_root in seen_roots or not robot_root.is_dir():
						continue
					seen_roots[robot_root] = None
					mcap_files = _confined_files(robot_root, ".mcap")
					if not mcap_files:
						continue

					uploaded_count = 0
					pending_count = 0
					failed_count = 0
					total_size = 0
					modified_times: List[float] = []
					for mcap_file in mcap_files:
						try:
							manifest = load_or_create_manifest(mcap_file)
							status = parse_status(manifest.get("status"), UploadStatus.PENDING)
							if status is UploadStatus.COMPLETED:
								uploaded_count += 1
							elif status is UploadStatus.FAILED:
								failed_count += 1
							else:
								pending_count += 1
							stat = mcap_file.stat()
							total_size += stat.st_size
							modified_times.append(stat.st_mtime)
						except (OSError, ValueError):
							pending_count += 1

					robots.append({
						"name": item.name,
						"path": str(robot_root),
						"mcap_count": len(mcap_files),
						"uploaded_count": uploaded_count,
						"pending_count": pending_count,
						"failed_count": failed_count,
						"total_size_bytes": total_size,
						"last_modified": max(modified_times, default=0),
					})
				
				# Sort by last modified (most recent first)
				robots.sort(key=lambda x: x["last_modified"], reverse=True)
			
			return {"robots": robots}
		except (OSError, ValueError):
			logger.warning("Could not discover robots below %s", BASE_ROOT, exc_info=True)
			return {"robots": []}

	return app


app = create_app()
