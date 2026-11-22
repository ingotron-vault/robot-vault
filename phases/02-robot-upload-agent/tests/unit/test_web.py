from pathlib import Path
from typing import List
import time

import pytest
from fastapi.testclient import TestClient

import robot_upload_agent.web as web
from robot_upload_agent.web import create_app
from robot_upload_agent.core import UploadQueue, UploadAgentConfig


# A complete MCAP envelope (leading magic, Footer record, closing magic).
# The upload gate treats a .mcap without its footer as still recording.
_MCAP_MAGIC = b"\x89MCAP0\r\n"
_MCAP_END = b"\x02" + (20).to_bytes(8, "little") + bytes(20) + _MCAP_MAGIC


def complete_mcap(payload: bytes) -> bytes:
	return _MCAP_MAGIC + payload + _MCAP_END



class FakeTransport:
	def __init__(self):
		self.calls = []

	def upload_small(self, manifest, data, compress=False):
		self.calls.append((manifest.get("path"), len(data)))
		return {"status": "completed"}

	def upload_stream(self, manifest, chunks, compress=False):
		n = 0
		for c in chunks:
			n += len(c)
		self.calls.append((manifest.get("path"), n))
		return {"status": "completed"}


def queue_factory(tmp_root: Path):
	# min_stable_age_seconds=0: the test uploads files it just wrote, which the
	# mtime stability gate would otherwise reject as still-recording.
	cfg = UploadAgentConfig(small_file_threshold_mb=1, chunk_size_bytes=5 * 1024 * 1024, min_stable_age_seconds=0)
	q = UploadQueue(root=tmp_root, config=cfg)
	q.transport = FakeTransport()
	return q


def test_status_and_files_and_upload(tmp_path: Path, monkeypatch):
	# Root confinement checks every `root=` against
	# the real BASE_ROOT (/tmp/robot_data); point it at tmp_path so this test
	# doesn't depend on that directory existing on the test host.
	monkeypatch.setattr(web, "BASE_ROOT", tmp_path)
	# Arrange: create some files in tmp root
	root = tmp_path / "robot" / "2025-08-14"
	root.mkdir(parents=True)
	f1 = root / "a.mcap"
	f2 = root / "b.mcap"
	f1.write_bytes(complete_mcap(b"x" * (256 * 1024)))
	f2.write_bytes(complete_mcap(b"y" * (2 * 1024 * 1024)))

	def _factory(p: Path) -> UploadQueue:
		return queue_factory(p)

	app = create_app(queue_factory=_factory)
	c = TestClient(app)

	# Status
	r = c.get("/api/status", params={"root": str(tmp_path / "robot")})
	assert r.status_code == 200
	js = r.json()
	assert js["root"].endswith("robot")

	# Files
	r2 = c.get("/api/files", params={"root": str(tmp_path / "robot")})
	assert r2.status_code == 200
	files = r2.json()
	paths = [i["path"] for i in files]
	assert str(f1) in paths and str(f2) in paths

	# Upload selected
	payload = {"root": str(tmp_path / "robot"), "files": [str(f1), str(f2)]}
	r3 = c.post("/api/upload/start", json=payload)
	assert r3.status_code == 200
	jid = r3.json()["job_id"]
	# Poll for completion with brief sleep to allow background thread to run
	j = None
	for _ in range(50):
		j = c.get(f"/api/upload/jobs/{jid}").json()
		if j["status"] == "completed":
			break
		time.sleep(0.01)
	# Completed
	assert j is not None and j["status"] == "completed"
	per_files = {f["path"]: f for f in j["files"]}
	# Job files report the canonical UploadStatus vocabulary.
	assert per_files[str(f1)]["status"] == "completed"
	assert per_files[str(f2)]["status"] == "completed"


def test_selected_job_preserves_success_after_precheck_failure(tmp_path: Path, monkeypatch):
	monkeypatch.setattr(web, "BASE_ROOT", tmp_path)
	root = tmp_path / "robot"
	root.mkdir()
	bad = root / "bad.mcap"
	good = root / "good.mcap"
	bad.write_bytes(complete_mcap(b"bad"))
	good.write_bytes(complete_mcap(b"good"))
	original = web.load_or_create_manifest

	def precheck(path):
		if path == bad:
			raise OSError("manifest unreadable")
		return original(path)

	monkeypatch.setattr(web, "load_or_create_manifest", precheck)
	client = TestClient(create_app(queue_factory=queue_factory))
	response = client.post("/api/upload/start", json={"root": str(root), "files": [str(bad), str(good)]})
	assert response.status_code == 200
	job_id = response.json()["job_id"]
	for _ in range(100):
		job = client.get(f"/api/upload/jobs/{job_id}").json()
		if job["status"] == "completed":
			break
		time.sleep(0.01)
	assert job["status"] == "completed"
	states = {entry["path"]: entry for entry in job["files"]}
	assert states[str(bad)]["status"] == "failed"
	assert states[str(bad)]["error"]["status"] == "preparation_failed"
	assert states[str(good)]["status"] == "completed"


# --- built SPA boundary (phase 02 technical guide: UI serving) -------------
#
# The Vite build is the only interactive browser client. A missing build fails
# closed with a fixed 503 page; the removed hand-written /static/app.js UI used
# innerHTML with manifest/path values and was a stored-XSS surface.

def _write_fake_spa_build(ui_dir: Path) -> None:
	(ui_dir / "assets").mkdir(parents=True)
	(ui_dir / "index.html").write_text(
		'<!doctype html><html><head><script type="module" src="/assets/index-abc123.js"></script></head>'
		'<body><div id="root"></div></body></html>'
	)
	(ui_dir / "assets" / "index-abc123.js").write_text("console.log('spa');")


def test_spa_is_served_when_the_build_output_exists(tmp_path: Path, monkeypatch):
	ui_dir = tmp_path / "ui"
	_write_fake_spa_build(ui_dir)
	monkeypatch.setattr(web, "UI_DIR", ui_dir)

	c = TestClient(web.create_app(queue_factory=lambda root: None))

	r = c.get("/")
	assert r.status_code == 200
	assert "id=\"root\"" in r.text
	assert "index-abc123.js" in r.text
	# The built shell's own filename never changes, so it must never be
	# cached -- only the content-hashed /assets/* below are safe to cache
	# forever.
	assert r.headers["cache-control"] == "no-cache, no-store, must-revalidate"

	r2 = c.get("/assets/index-abc123.js")
	assert r2.status_code == 200
	assert "spa" in r2.text

	# The real API surface must still win over the SPA mount for its own
	# paths -- the mount only ever catches "/" and "/assets/*".
	r3 = c.get("/api/robots/discover")
	assert r3.status_code == 200


def test_spa_asset_mount_rejects_a_symlink_escape(tmp_path: Path, monkeypatch):
	ui_dir = tmp_path / "ui"
	_write_fake_spa_build(ui_dir)
	secret = tmp_path / "secret.js"
	secret.write_text("outside-secret")
	(ui_dir / "assets" / "leak.js").symlink_to(secret)
	monkeypatch.setattr(web, "UI_DIR", ui_dir)

	c = TestClient(web.create_app(queue_factory=lambda root: None))
	r = c.get("/assets/leak.js")
	assert r.status_code == 404
	assert "outside-secret" not in r.text


def test_missing_spa_build_fails_closed_without_legacy_javascript(tmp_path: Path, monkeypatch):
	monkeypatch.setattr(web, "UI_DIR", tmp_path / "never-built")

	c = TestClient(web.create_app(queue_factory=lambda root: None))

	r = c.get("/")
	assert r.status_code == 503
	assert "Robot Upload UI is not built" in r.text
	assert "/static/app.js" not in r.text
	assert r.headers["cache-control"] == "no-store"
	assert r.headers["content-security-policy"] == "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
	assert c.get("/static/app.js").status_code == 404


# --- root confinement ------------------------------------------------------
#
# The phase 02 technical guide's "Web filesystem boundary" requires all six
# endpoints that accept a `root` to use the same confinement policy: /api/status,
# /api/files, POST /api/upload/start, /api/upload/history,
# POST /api/upload/resume-all and POST /api/upload/retry-failed. All six now
# route through `_resolve_confined_path`, which requires the resolved root to
# be BASE_ROOT itself or somewhere inside it.

# (method, path, kwargs-builder) for each of the six root-accepting endpoints.
_ROOT_ENDPOINTS = [
	("GET", "/api/status", lambda root: {"params": {"root": root}}),
	("GET", "/api/files", lambda root: {"params": {"root": root}}),
	("POST", "/api/upload/start", lambda root: {"json": {"root": root, "files": ["x"]}}),
	("GET", "/api/upload/history", lambda root: {"params": {"root": root}}),
	("POST", "/api/upload/resume-all", lambda root: {"json": {"root": root}}),
	("POST", "/api/upload/retry-failed", lambda root: {"json": {"root": root}}),
]


@pytest.mark.parametrize("method,path,build_kwargs", _ROOT_ENDPOINTS)
def test_root_confinement_rejects_outside_base_on_every_endpoint(method, path, build_kwargs, tmp_path: Path, monkeypatch):
	base = tmp_path / "robot_data"
	base.mkdir()
	outside = tmp_path / "outside"
	outside.mkdir()
	monkeypatch.setattr(web, "BASE_ROOT", base)

	c = TestClient(web.create_app(queue_factory=lambda root: None))
	r = c.request(method, path, **build_kwargs(str(outside)))
	assert r.status_code == 400, r.text


def test_root_confinement_rejects_dotdot_traversal_against_the_real_base_root():
	# Deliberately no monkeypatch: exercises the actual default BASE_ROOT
	# (/tmp/robot_data), not a test-local stand-in. `Path.resolve()` doesn't
	# require the path to exist, so this works even on a host with no
	# /tmp/robot_data directory.
	c = TestClient(web.create_app(queue_factory=lambda root: None))
	r = c.get("/api/status", params={"root": "/tmp/robot_data/../etc"})
	assert r.status_code == 400, r.text


def test_root_confinement_rejects_an_unrelated_absolute_path():
	c = TestClient(web.create_app(queue_factory=lambda root: None))
	r = c.get("/api/files", params={"root": "/etc"})
	assert r.status_code == 400, r.text


def test_root_confinement_rejects_a_symlink_escape(tmp_path: Path, monkeypatch):
	# A symlink planted *inside* BASE_ROOT that points outside it would slip
	# past a plain string-prefix check; `Path.resolve()` follows the symlink
	# before the containment check runs, so it doesn't.
	base = tmp_path / "robot_data"
	base.mkdir()
	outside = tmp_path / "outside"
	outside.mkdir()
	escape = base / "escape"
	escape.symlink_to(outside, target_is_directory=True)
	monkeypatch.setattr(web, "BASE_ROOT", base)

	c = TestClient(web.create_app(queue_factory=lambda root: None))
	r = c.get("/api/status", params={"root": str(escape)})
	assert r.status_code == 400, r.text


def test_root_confinement_allows_base_itself(tmp_path: Path, monkeypatch):
	monkeypatch.setattr(web, "BASE_ROOT", tmp_path)

	c = TestClient(web.create_app(queue_factory=lambda root: None))
	r = c.get("/api/upload/history", params={"root": str(tmp_path)})
	assert r.status_code == 200, r.text


def test_root_confinement_allows_a_child_of_base(tmp_path: Path, monkeypatch):
	monkeypatch.setattr(web, "BASE_ROOT", tmp_path)
	child = tmp_path / "rover-1"
	child.mkdir()

	c = TestClient(web.create_app(queue_factory=lambda root: None))
	r = c.get("/api/upload/history", params={"root": str(child)})
	assert r.status_code == 200, r.text

	# No /assets mount was registered in this branch.
	r2 = c.get("/assets/whatever.js")
	assert r2.status_code == 404
