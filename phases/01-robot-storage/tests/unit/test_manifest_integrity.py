"""Tests for robot-side manifest integrity.

Covers: atomic manifest writes (no torn writes on crash), stale-status reset
when file content changes since the last scan, CLI retention flags actually
reaching RetentionPlanner, never-uploaded files being protected from deletion
by default, the package-level __init__.py export/import fix, and reconciling
manifests stranded at status=='uploading' by a hard-killed upload process.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from robot_storage_manager.cli import main
from robot_storage_manager.core import (
	ConcurrentManifestUpdateError,
	FileChangedDuringHashError,
	MAX_MANIFEST_BYTES,
	RetentionPlanner,
	compute_sha256_for_file,
	delete_retention_item,
	is_file_stable,
	load_or_create_manifest,
	manifest_path_for,
	save_manifest,
)


# A complete MCAP envelope (leading magic, Footer record, closing magic).
# The upload gate treats a .mcap without its footer as still recording.
_MCAP_MAGIC = b"\x89MCAP0\r\n"
_MCAP_END = b"\x02" + (20).to_bytes(8, "little") + bytes(20) + _MCAP_MAGIC


def complete_mcap(payload: bytes) -> bytes:
	return _MCAP_MAGIC + payload + _MCAP_END



def test_save_manifest_atomic_write_survives_crash_mid_write(tmp_path: Path, monkeypatch):
	"""A crash between the temp-file write and the rename must not corrupt
	the manifest: the old manifest stays intact and no partial temp file
	is left behind.
	"""
	f = tmp_path / "a.mcap"
	f.write_bytes(b"hello world")

	mf = load_or_create_manifest(f)
	mf["status"] = "completed"
	mf["upload_id"] = "abc-123"
	mf["sha256_hash"] = "deadbeef"
	save_manifest(mf)

	mf_path = manifest_path_for(f)
	original_bytes = mf_path.read_bytes()

	# Simulate a crash: the rename step (os.replace) never completes.
	import robot_storage_manager.core as core_mod

	def boom(*_args, **_kwargs):
		raise OSError("simulated power loss during rename")

	monkeypatch.setattr(core_mod.os, "replace", boom)

	mf["status"] = "uploading"
	with pytest.raises(OSError):
		save_manifest(mf)

	# The old manifest must be untouched -- not truncated, not partially
	# overwritten.
	assert mf_path.read_bytes() == original_bytes
	reloaded = json.loads(mf_path.read_text())
	assert reloaded["status"] == "completed"
	assert reloaded["upload_id"] == "abc-123"

	# No leftover partial/temp file in the directory.
	leftovers = [
		p for p in mf_path.parent.iterdir()
		if p.name.startswith(mf_path.name + ".") and p.name.endswith(".tmp")
	]
	assert leftovers == []


def test_save_manifest_round_trips_normally(tmp_path: Path):
	"""Sanity check that the new atomic-write path still produces a readable,
	correct manifest on the (non-crash) happy path.
	"""
	f = tmp_path / "b.mcap"
	f.write_bytes(b"payload")
	mf = load_or_create_manifest(f)
	mf["status"] = "completed"
	save_manifest(mf)

	reloaded = json.loads(manifest_path_for(f).read_text())
	assert reloaded["status"] == "completed"
	assert reloaded["file_size_bytes"] == f.stat().st_size


def test_unreadable_manifest_fails_closed_instead_of_being_replaced(
	tmp_path: Path, monkeypatch
):
	data_file = tmp_path / "unreadable.mcap"
	data_file.write_bytes(b"payload")
	manifest_file = manifest_path_for(data_file)
	manifest_file.write_text('{"status":"completed"}')
	original = manifest_file.read_bytes()

	import robot_storage_manager.core as core_mod

	real_read_manifest_snapshot = core_mod._read_manifest_raw_snapshot

	def deny_manifest(path):
		if path == manifest_file:
			raise PermissionError("temporarily unreadable")
		return real_read_manifest_snapshot(path)

	monkeypatch.setattr(core_mod, "_read_manifest_raw_snapshot", deny_manifest)
	with pytest.raises(PermissionError, match="temporarily unreadable"):
		load_or_create_manifest(data_file)

	assert manifest_file.read_bytes() == original


def test_corrupt_copy_does_not_follow_existing_symlink(tmp_path: Path):
	data_file = tmp_path / "corrupt.mcap"
	data_file.write_bytes(b"payload")
	manifest_file = manifest_path_for(data_file)
	manifest_file.write_text("{not json")

	outside = tmp_path / "outside.txt"
	outside.write_text("must remain unchanged")
	manifest_file.with_name(manifest_file.name + ".corrupt").symlink_to(outside)

	manifest = load_or_create_manifest(data_file)
	assert manifest["status"] == "pending"
	assert outside.read_text() == "must remain unchanged"


@pytest.mark.parametrize(
	"payload",
	[
		b'{"status":"pending","status":"completed"}',
		b'{"status":"completed","file_size_bytes":NaN}',
	],
)
def test_manifest_json_rejects_duplicate_keys_and_non_finite_numbers(
	tmp_path: Path, payload: bytes
):
	data_file = tmp_path / "strict.mcap"
	data_file.write_bytes(b"payload")
	manifest_file = manifest_path_for(data_file)
	manifest_file.write_bytes(payload)

	manifest = load_or_create_manifest(data_file)

	assert manifest["status"] == "pending"
	assert manifest_file.with_name(manifest_file.name + ".corrupt").read_bytes() == payload


def test_oversized_manifest_is_bounded_and_regenerated(tmp_path: Path):
	data_file = tmp_path / "oversized.mcap"
	data_file.write_bytes(b"payload")
	manifest_file = manifest_path_for(data_file)
	manifest_file.write_bytes(b"{" + b"x" * (MAX_MANIFEST_BYTES + 100))

	manifest = load_or_create_manifest(data_file)

	assert manifest["status"] == "pending"
	corrupt = manifest_file.with_name(manifest_file.name + ".corrupt")
	assert MAX_MANIFEST_BYTES < corrupt.stat().st_size < MAX_MANIFEST_BYTES + 200


def test_manifest_symlink_is_never_followed(tmp_path: Path):
	data_file = tmp_path / "linked-manifest.mcap"
	data_file.write_bytes(b"payload")
	outside = tmp_path / "outside.json"
	outside.write_text('{"status":"completed"}')
	manifest_path_for(data_file).symlink_to(outside)

	with pytest.raises(OSError):
		load_or_create_manifest(data_file)

	assert outside.read_text() == '{"status":"completed"}'


def test_root_anchored_save_rejects_parent_directory_symlink_swap(tmp_path: Path):
	root = tmp_path / "capture"
	day = root / "day"
	day.mkdir(parents=True)
	data_file = day / "capture.mcap"
	data_file.write_bytes(b"inside")
	manifest = load_or_create_manifest(data_file, root=root)

	moved = tmp_path / "moved"
	day.rename(moved)
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / data_file.name).write_bytes(b"outside")
	day.symlink_to(outside, target_is_directory=True)

	with pytest.raises(OSError):
		save_manifest(manifest, root=root)

	assert not manifest_path_for(outside / data_file.name).exists()
	assert (moved / data_file.name).read_bytes() == b"inside"


def test_root_anchored_load_rejects_parent_swap_before_open(
	tmp_path: Path, monkeypatch
):
	root = tmp_path / "capture"
	day = root / "day"
	day.mkdir(parents=True)
	data_file = day / "capture.mcap"
	data_file.write_bytes(b"inside")
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / data_file.name).write_bytes(b"outside")
	manifest_path_for(outside / data_file.name).write_text("{not-json")
	moved = tmp_path / "moved"

	import robot_storage_manager.core as core_mod

	real_open_parent = core_mod._open_parent_beneath
	swapped = False

	def swap_then_open(anchor, target):
		nonlocal swapped
		if not swapped:
			swapped = True
			day.rename(moved)
			day.symlink_to(outside, target_is_directory=True)
		return real_open_parent(anchor, target)

	monkeypatch.setattr(core_mod, "_open_parent_beneath", swap_then_open)
	with pytest.raises(OSError):
		load_or_create_manifest(data_file, root=root)

	assert not manifest_path_for(outside / data_file.name).with_name(
		manifest_path_for(outside / data_file.name).name + ".corrupt"
	).exists()


def test_root_anchored_hash_rejects_parent_swap_before_open(
	tmp_path: Path, monkeypatch
):
	root = tmp_path / "capture"
	day = root / "day"
	day.mkdir(parents=True)
	data_file = day / "capture.mcap"
	data_file.write_bytes(b"inside")
	outside = tmp_path / "hash-outside"
	outside.mkdir()
	(outside / data_file.name).write_bytes(b"outside-secret")
	moved = tmp_path / "hash-moved"

	import robot_storage_manager.core as core_mod

	real_open_parent = core_mod._open_parent_beneath
	swapped = False

	def swap_then_open(anchor, target):
		nonlocal swapped
		if not swapped:
			swapped = True
			day.rename(moved)
			day.symlink_to(outside, target_is_directory=True)
		return real_open_parent(anchor, target)

	monkeypatch.setattr(core_mod, "_open_parent_beneath", swap_then_open)
	with pytest.raises(OSError):
		compute_sha256_for_file(data_file, root=root)


def test_stale_manifest_writer_cannot_overwrite_newer_upload_state(tmp_path: Path):
	data_file = tmp_path / "concurrent.mcap"
	data_file.write_bytes(b"payload")
	first = load_or_create_manifest(data_file)
	stale = load_or_create_manifest(data_file)

	first["status"] = "uploading"
	save_manifest(first)
	stale["status"] = "pending"
	with pytest.raises(ConcurrentManifestUpdateError, match="stale manifest revision"):
		save_manifest(stale)

	assert load_or_create_manifest(data_file)["status"] == "uploading"


def test_completed_state_is_sticky_against_a_stale_writer(tmp_path: Path):
	data_file = tmp_path / "completed-race.mcap"
	data_file.write_bytes(b"payload")
	initial = load_or_create_manifest(data_file)
	save_manifest(initial)
	winner = load_or_create_manifest(data_file)
	stale = load_or_create_manifest(data_file)

	winner.update(status="completed", sha256_hash="a" * 64, upload_id="upload-1")
	save_manifest(winner)
	stale["status"] = "failed"
	save_manifest(stale)  # successful no-op; completion must remain authoritative

	assert stale["status"] == "completed"
	assert load_or_create_manifest(data_file)["status"] == "completed"


def test_unchanged_scan_does_not_churn_manifest_revision(tmp_path: Path):
	data_file = tmp_path / "idempotent.mcap"
	data_file.write_bytes(b"payload")
	manifest = load_or_create_manifest(data_file)
	save_manifest(manifest)
	first_revision = manifest["manifest_revision"]
	first_bytes = manifest_path_for(data_file).read_bytes()

	reloaded = load_or_create_manifest(data_file)
	save_manifest(reloaded)

	assert reloaded["manifest_revision"] == first_revision
	assert manifest_path_for(data_file).read_bytes() == first_bytes


def test_hash_fails_if_file_changes_during_read(tmp_path: Path, monkeypatch):
	data_file = tmp_path / "growing.mcap"
	data_file.write_bytes(b"abcdefgh")
	import robot_storage_manager.core as core_mod

	real_sha256 = core_mod.hashlib.sha256

	class MutatingHash:
		def __init__(self):
			self.inner = real_sha256()
			self.mutated = False

		def update(self, chunk):
			self.inner.update(chunk)
			if not self.mutated:
				self.mutated = True
				data_file.write_bytes(b"ABCDEFGHI")

		def hexdigest(self):
			return self.inner.hexdigest()

	monkeypatch.setattr(core_mod.hashlib, "sha256", MutatingHash)
	with pytest.raises(FileChangedDuringHashError, match="changed while"):
		compute_sha256_for_file(data_file, chunk_size=1)


def test_load_or_create_manifest_resets_status_on_content_change(tmp_path: Path):
	"""If the underlying file's size/mtime changed since the manifest was
	last written, the manifest no longer describes the current content --
	status must reset to pending and the hash/upload_id must be cleared.
	"""
	f = tmp_path / "c.mcap"
	f.write_bytes(b"initial content")

	mf = load_or_create_manifest(f)
	mf["status"] = "completed"
	mf["sha256_hash"] = "0" * 64
	mf["upload_id"] = "up-1"
	save_manifest(mf)

	# Sanity: re-loading with no change to the file preserves status/hash.
	unchanged = load_or_create_manifest(f)
	assert unchanged["status"] == "completed"
	assert unchanged["sha256_hash"] == "0" * 64
	assert unchanged["upload_id"] == "up-1"

	# Simulate the robot still recording / re-recording this file: both size
	# and mtime change.
	time.sleep(0.01)
	f.write_bytes(b"initial content plus more appended recording bytes")

	reloaded = load_or_create_manifest(f)
	assert reloaded["status"] == "pending"
	assert reloaded["sha256_hash"] is None
	assert reloaded["upload_id"] is None
	assert reloaded["file_size_bytes"] == f.stat().st_size
	assert reloaded["mtime_ns"] == f.stat().st_mtime_ns


def test_load_or_create_manifest_resets_on_mtime_only_change(tmp_path: Path):
	"""Even same-size content changes (mtime moves, size doesn't) must not be
	trusted as still-valid -- e.g. a file rewritten with different content of
	the same length.
	"""
	f = tmp_path / "d.mcap"
	f.write_bytes(b"AAAAAAAAAA")

	mf = load_or_create_manifest(f)
	mf["status"] = "completed"
	mf["sha256_hash"] = "1" * 64
	save_manifest(mf)

	time.sleep(0.01)
	f.write_bytes(b"BBBBBBBBBB")  # same size, different bytes, new mtime
	assert f.stat().st_size == len(b"AAAAAAAAAA")

	reloaded = load_or_create_manifest(f)
	assert reloaded["status"] == "pending"
	assert reloaded["sha256_hash"] is None


def test_load_or_create_manifest_resets_stale_uploading_to_failed(tmp_path: Path):
	"""If the upload agent's process was hard-killed (SIGTERM/crash) mid
	upload, the except block that would normally revert status='uploading'
	to 'failed' never runs -- the manifest is left stuck at 'uploading'
	forever, and since the upload loop only re-selects pending/failed files,
	that file is silently stranded (never re-uploaded).

	When the manifest file's own mtime (last time 'uploading' was written)
	is older than the staleness threshold, load_or_create_manifest must
	reset status back to 'failed' so the upload loop re-selects it, while
	preserving the sha256_hash (the file content hasn't changed, only the
	upload attempt died).
	"""
	f = tmp_path / "stranded.mcap"
	f.write_bytes(b"data that was mid-upload when the process was killed")

	mf = load_or_create_manifest(f)
	mf["status"] = "uploading"
	mf["sha256_hash"] = "a" * 64
	mf["upload_id"] = "up-stranded"
	save_manifest(mf)

	mf_path = manifest_path_for(f)
	# Simulate the manifest having sat at 'uploading' for a long time by
	# backdating the manifest file's own mtime, then evaluate "now" as far
	# enough past that mtime to exceed the (small, test-provided) threshold.
	old_mtime = mf_path.stat().st_mtime - 7200  # 2 hours in the past
	os.utime(mf_path, (old_mtime, old_mtime))

	reloaded = load_or_create_manifest(
		f, stale_uploading_seconds=3600.0, now=old_mtime + 7200
	)
	assert reloaded["status"] == "failed"
	# The hash is still valid -- content didn't change, only the upload
	# attempt died -- so it must NOT be cleared.
	assert reloaded["sha256_hash"] == "a" * 64

	# The file must now be a legitimate upload candidate again (i.e. the
	# reconciled status is one the upload loop treats as pending/failed).
	assert reloaded["status"] in ("pending", "failed")


def test_load_or_create_manifest_preserves_fresh_uploading(tmp_path: Path):
	"""A manifest that was *just* set to 'uploading' (fresh mtime) must be
	left alone -- resetting it would clobber an actively in-progress upload
	from a concurrent run, not just a stranded one.
	"""
	f = tmp_path / "in_flight.mcap"
	f.write_bytes(b"data currently being streamed by a live upload process")

	mf = load_or_create_manifest(f)
	mf["status"] = "uploading"
	mf["sha256_hash"] = "b" * 64
	mf["upload_id"] = "up-live"
	save_manifest(mf)

	mf_path = manifest_path_for(f)
	fresh_mtime = mf_path.stat().st_mtime

	# "now" is only a few seconds after the manifest was written -- well
	# under the staleness threshold -- so this looks like an active upload.
	reloaded = load_or_create_manifest(
		f, stale_uploading_seconds=3600.0, now=fresh_mtime + 5.0
	)
	assert reloaded["status"] == "uploading"
	assert reloaded["sha256_hash"] == "b" * 64
	assert reloaded["upload_id"] == "up-live"


def test_load_or_create_manifest_size_mtime_reset_still_works_alongside_stale_uploading_check(tmp_path: Path):
	"""Sanity/regression check: adding the stale-'uploading' reconciliation
	must not disturb the pre-existing invariant that a content change (size
	or mtime) resets status to 'pending' and clears hash/upload_id -- even
	when the manifest's previous status was 'uploading'.
	"""
	f = tmp_path / "changed_while_uploading.mcap"
	f.write_bytes(b"original content")

	mf = load_or_create_manifest(f)
	mf["status"] = "uploading"
	mf["sha256_hash"] = "c" * 64
	mf["upload_id"] = "up-changed"
	save_manifest(mf)

	# Content changes underneath the stuck 'uploading' manifest (e.g. robot
	# re-recorded/truncated the file) -- this must win over the stale-upload
	# reconciliation and always reset to pending, regardless of how fresh or
	# stale the manifest mtime is.
	time.sleep(0.01)
	f.write_bytes(b"different content, different length now")

	reloaded = load_or_create_manifest(f)
	assert reloaded["status"] == "pending"
	assert reloaded["sha256_hash"] is None
	assert reloaded["upload_id"] is None
	assert reloaded["file_size_bytes"] == f.stat().st_size


def test_is_file_stable_gate(tmp_path: Path):
	"""Files modified within the stability window are not yet safe to hash
	or upload; older files are.
	"""
	f = tmp_path / "e.mcap"
	f.write_bytes(MCAP_MAGIC + b"recording..." + MCAP_FOOTER + MCAP_MAGIC)

	now = f.stat().st_mtime
	# Just modified: well within a 5s stability window.
	assert is_file_stable(f, min_age_seconds=5.0, now=now + 0.1) is False
	# Old enough: outside the window.
	assert is_file_stable(f, min_age_seconds=5.0, now=now + 10.0) is True

	# Missing files are considered stable (nothing to wait on).
	assert is_file_stable(tmp_path / "missing.mcap") is True


MCAP_MAGIC = b"\x89MCAP0\r\n"
# Footer record: opcode 0x02, length 20, then summary_start (u64),
# summary_offset_start (u64) and summary_crc (u32).
MCAP_FOOTER = b"\x02" + (20).to_bytes(8, "little") + bytes(20)


def test_is_file_stable_requires_a_complete_mcap_footer(tmp_path: Path):
	"""A recorder that buffers can pause for longer than the mtime window.
	The mtime check alone would then ship a truncated MCAP. Ways this check
	could fail, each covered below:

	1. The file has no trailing magic (truncated mid-write).
	2. The trailing magic is present, but no Footer record precedes it.
	3. The file is shorter than a footer plus its magic.
	4. The check leaks to other extensions and blocks them.
	5. An upper-case ``.MCAP`` name skips the check.
	6. ``min_age_seconds=0`` also disables the footer check.
	7. A complete file is still gated by its mtime.
	"""
	now = time.time() + 3600

	truncated = tmp_path / "truncated.mcap"
	truncated.write_bytes(MCAP_MAGIC + b"records...")
	assert is_file_stable(truncated, min_age_seconds=5.0, now=now) is False
	assert is_file_stable(truncated, min_age_seconds=0, now=now) is False

	no_footer = tmp_path / "no_footer.mcap"
	no_footer.write_bytes(MCAP_MAGIC + bytes(40) + MCAP_MAGIC)
	assert is_file_stable(no_footer, min_age_seconds=5.0, now=now) is False

	tiny = tmp_path / "tiny.mcap"
	tiny.write_bytes(MCAP_MAGIC)
	assert is_file_stable(tiny, min_age_seconds=5.0, now=now) is False

	complete = tmp_path / "complete.MCAP"
	complete.write_bytes(MCAP_MAGIC + b"records..." + MCAP_FOOTER + MCAP_MAGIC)
	assert is_file_stable(complete, min_age_seconds=5.0, now=now) is True
	assert is_file_stable(complete, min_age_seconds=0, now=now) is True
	mtime = complete.stat().st_mtime
	assert is_file_stable(complete, min_age_seconds=5.0, now=mtime + 0.1) is False

	other = tmp_path / "notes.bag"
	other.write_bytes(b"not an mcap")
	assert is_file_stable(other, min_age_seconds=5.0, now=now) is True


def test_cli_retention_apply_honors_min_retain_days_flag(tmp_path: Path, capsys):
	"""rsm retention apply --min-retain-days 30 must actually reach
	RetentionPlanner and protect an 8-day-old file, not silently fall back
	to the 7-day dataclass default.
	"""
	root = tmp_path / "robot" / "2026-07-01"
	root.mkdir(parents=True)
	f = root / "old_upload.mcap"
	f.write_bytes(b"x" * (5 * 1024 * 1024))  # 5 MiB, uploaded, over any tiny cap

	now = datetime(2026, 7, 21, tzinfo=timezone.utc)
	eight_days_ago = (now - timedelta(days=8)).timestamp()
	os.utime(f, (eight_days_ago, eight_days_ago))

	mf = load_or_create_manifest(f)
	mf["status"] = "completed"
	mf["sha256_hash"] = "a" * 64
	mf["upload_id"] = "upload-old"
	save_manifest(mf)

	now_iso = now.isoformat()

	# Control: default 7-day retention considers an 8-day-old file eligible,
	# so a tiny size cap forces it into the deletion plan.
	rc = main([
		"retention", "apply",
		"--root", str(root),
		"--max-total-gib", "0.000001",
		"--now", now_iso,
	])
	assert rc == 0
	control = json.loads(capsys.readouterr().out)
	assert control["to_delete"], "sanity check: default 7-day policy should flag the 8-day-old file"

	# With --min-retain-days 30, the same 8-day-old file must be protected.
	rc = main([
		"retention", "apply",
		"--root", str(root),
		"--max-total-gib", "0.000001",
		"--min-retain-days", "30",
		"--now", now_iso,
	])
	assert rc == 0
	protected = json.loads(capsys.readouterr().out)
	assert protected["to_delete"] == []


def test_cli_retention_plan_honors_max_total_gib_flag(tmp_path: Path, capsys):
	"""rsm retention plan --max-total-gib must also reach RetentionPlanner
	(not just --min-retain-days), overriding the 200 GiB dataclass default.
	"""
	root = tmp_path / "robot" / "2026-07-01"
	root.mkdir(parents=True)
	f = root / "small_upload.mcap"
	f.write_bytes(b"x" * (5 * 1024 * 1024))  # 5 MiB -- well over the tiny cap below
	old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
	os.utime(f, (old, old))
	mf = load_or_create_manifest(f)
	mf["status"] = "completed"
	mf["sha256_hash"] = "b" * 64
	mf["upload_id"] = "upload-small"
	save_manifest(mf)

	# Default 200 GiB cap: nothing to delete.
	rc = main(["retention", "plan", "--root", str(root)])
	assert rc == 0
	default_out = json.loads(capsys.readouterr().out)
	assert default_out["to_delete"] == []

	# A tiny explicit cap must be honored and force a deletion.
	rc = main(["retention", "plan", "--root", str(root), "--max-total-gib", "0.000001"])
	assert rc == 0
	capped_out = json.loads(capsys.readouterr().out)
	assert len(capped_out["to_delete"]) == 1


def test_cli_retention_never_uploaded_protected_unless_allow_flag(tmp_path: Path, capsys):
	"""Never-uploaded (pending/failed) files must not appear in the deletion
	plan unless --allow-unuploaded is passed, and must always be reported as
	an at-risk count/bytes so operators can see what's protected.
	"""
	root = tmp_path / "robot" / "2026-06-01"
	root.mkdir(parents=True)
	f = root / "still_pending.mcap"
	f.write_bytes(b"x" * (5 * 1024 * 1024))  # 5 MiB
	old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
	os.utime(f, (old, old))
	mf = load_or_create_manifest(f)
	mf["status"] = "pending"
	save_manifest(mf)

	# Default: protected from deletion, but surfaced as at-risk.
	rc = main(["retention", "plan", "--root", str(root), "--max-total-gib", "0.000001"])
	assert rc == 0
	out = json.loads(capsys.readouterr().out)
	assert out["to_delete"] == []
	assert out["allow_unuploaded"] is False
	assert out["never_uploaded_at_risk_count"] == 1
	assert out["never_uploaded_at_risk_bytes"] == f.stat().st_size

	# Explicit opt-in: now it's included in the deletion plan.
	rc = main([
		"retention", "plan",
		"--root", str(root),
		"--max-total-gib", "0.000001",
		"--allow-unuploaded",
	])
	assert rc == 0
	out2 = json.loads(capsys.readouterr().out)
	assert len(out2["to_delete"]) == 1
	assert out2["to_delete"][0]["status"] == "pending"
	assert out2["never_uploaded_at_risk_count"] == 0


def _verified_old_upload(path: Path) -> dict:
	old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
	os.utime(path, (old, old))
	manifest = load_or_create_manifest(path)
	manifest.update(
		status="completed",
		sha256_hash="e" * 64,
		upload_id=f"upload-{path.name}",
	)
	save_manifest(manifest)
	return manifest


def test_completed_without_integrity_evidence_is_not_deletion_eligible(tmp_path: Path):
	data_file = tmp_path / "unverified.mcap"
	data_file.write_bytes(b"x" * 4096)
	old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
	os.utime(data_file, (old, old))
	manifest = load_or_create_manifest(data_file)
	manifest["status"] = "completed"
	save_manifest(manifest)

	plan = RetentionPlanner(min_retain_days=0, max_total_gib=0.000001).plan([manifest])

	assert plan["to_delete"] == []
	assert [item["path"] for item in plan["unverified_completed"]] == [str(data_file)]
	assert plan["capacity_target_met"] is False
	assert plan["capacity_shortfall_bytes"] > 0


def test_hardlinked_file_is_reported_and_not_counted_as_freed(tmp_path: Path):
	data_file = tmp_path / "hardlinked.mcap"
	data_file.write_bytes(b"x" * 4096)
	linked_name = tmp_path / "other-name.bin"
	os.link(data_file, linked_name)
	manifest = _verified_old_upload(data_file)

	plan = RetentionPlanner(min_retain_days=0, max_total_gib=0.000001).plan([manifest])

	assert plan["to_delete"] == []
	assert [item["path"] for item in plan["protected_hardlinks"]] == [str(data_file)]
	assert plan["capacity_target_met"] is False
	assert linked_name.exists()


@pytest.mark.parametrize(
	"kwargs",
	[
		{"min_retain_days": -1},
		{"max_total_gib": -1},
		{"max_total_gib": float("nan")},
		{"max_total_gib": float("inf")},
		{"now_override": "2026-08-09T12:00:00"},
	],
)
def test_retention_planner_rejects_unsafe_policy_values(kwargs):
	with pytest.raises(ValueError):
		RetentionPlanner(**kwargs)


@pytest.mark.parametrize(
	"flag,value",
	[
		("--max-total-gib", "nan"),
		("--max-total-gib", "-1"),
		("--min-retain-days", "-1"),
		("--now", "2026-08-09T12:00:00"),
	],
)
def test_cli_rejects_unsafe_retention_values(tmp_path: Path, flag: str, value: str):
	with pytest.raises(SystemExit):
		main(["retention", "plan", "--root", str(tmp_path), flag, value])


def test_cli_rejects_glob_extension_and_missing_root(tmp_path: Path):
	with pytest.raises(SystemExit):
		main(["scan", "--root", str(tmp_path), "--ext", ".*"])
	with pytest.raises(SystemExit):
		main(["scan", "--root", str(tmp_path / "typo")])


def test_hash_cli_skips_fresh_files_unless_operator_overrides_gate(
	tmp_path: Path, capsys
):
	data_file = tmp_path / "fresh.mcap"
	data_file.write_bytes(complete_mcap(b"payload"))

	assert main(["hash", "--root", str(tmp_path)]) == 0
	first = json.loads(capsys.readouterr().out)
	assert first["files_hashed"] == 0
	assert first["files_skipped_unstable"] == [str(data_file)]

	assert main([
		"hash", "--root", str(tmp_path), "--min-stable-age-seconds", "0"
	]) == 0
	second = json.loads(capsys.readouterr().out)
	assert second["files_hashed"] == 1
	assert len(load_or_create_manifest(data_file)["sha256_hash"]) == 64

	invalid = load_or_create_manifest(data_file)
	invalid["sha256_hash"] = "not-a-sha256"
	save_manifest(invalid)
	assert main([
		"hash", "--root", str(tmp_path), "--missing-only",
		"--min-stable-age-seconds", "0",
	]) == 0
	third = json.loads(capsys.readouterr().out)
	assert third["files_hashed"] == 1
	assert len(load_or_create_manifest(data_file)["sha256_hash"]) == 64


def test_retention_refuses_replacement_file_after_plan(tmp_path: Path):
	root = tmp_path / "robot"
	root.mkdir()
	data_file = root / "replace.mcap"
	data_file.write_bytes(b"original")
	manifest = _verified_old_upload(data_file)
	plan = RetentionPlanner(min_retain_days=0, max_total_gib=0.000000001).plan([manifest])
	item = plan["to_delete"][0]
	original_mtime_ns = data_file.stat().st_mtime_ns
	replacement = root / "replacement.tmp"
	replacement.write_bytes(b"new-data")
	os.utime(replacement, ns=(original_mtime_ns, original_mtime_ns))
	os.replace(replacement, data_file)

	with pytest.raises(OSError, match="changed after retention planning"):
		delete_retention_item(root, item)

	assert data_file.read_bytes() == b"new-data"


def test_retention_refuses_manifest_update_after_plan(tmp_path: Path):
	root = tmp_path / "robot"
	root.mkdir()
	data_file = root / "status-race.mcap"
	data_file.write_bytes(b"payload")
	manifest = _verified_old_upload(data_file)
	plan = RetentionPlanner(min_retain_days=0, max_total_gib=0.000000001).plan([manifest])
	item = plan["to_delete"][0]
	current = load_or_create_manifest(data_file)
	current["status"] = "uploading"
	save_manifest(current)

	with pytest.raises(OSError, match="manifest changed after retention planning"):
		delete_retention_item(root, item)

	assert data_file.exists()


def test_retention_parent_symlink_swap_cannot_delete_outside_root(tmp_path: Path):
	root = tmp_path / "robot"
	day = root / "day"
	day.mkdir(parents=True)
	data_file = day / "capture.mcap"
	data_file.write_bytes(b"inside")
	manifest = _verified_old_upload(data_file)
	plan = RetentionPlanner(min_retain_days=0, max_total_gib=0.000000001).plan([manifest])
	item = plan["to_delete"][0]

	moved = tmp_path / "moved-day"
	day.rename(moved)
	outside = tmp_path / "outside"
	outside.mkdir()
	outside_file = outside / data_file.name
	outside_file.write_bytes(b"must survive")
	day.symlink_to(outside, target_is_directory=True)

	with pytest.raises(OSError):
		delete_retention_item(root, item)

	assert outside_file.read_bytes() == b"must survive"
	assert (moved / data_file.name).read_bytes() == b"inside"


def test_root_anchored_retention_plan_marks_parent_swap_incomplete(tmp_path: Path):
	root = tmp_path / "robot"
	day = root / "day"
	day.mkdir(parents=True)
	data_file = day / "capture.mcap"
	data_file.write_bytes(b"inside")
	manifest = _verified_old_upload(data_file)
	moved = tmp_path / "moved-plan-day"
	day.rename(moved)
	outside = tmp_path / "outside-plan"
	outside.mkdir()
	(outside / data_file.name).write_bytes(b"must survive")
	day.symlink_to(outside, target_is_directory=True)

	plan = RetentionPlanner(min_retain_days=0, max_total_gib=0.000000001).plan(
		[manifest], root=root
	)

	assert plan["to_delete"] == []
	assert plan["measurement_complete"] is False
	assert plan["capacity_target_met"] is False
	assert plan["unreadable"]
	assert (outside / data_file.name).read_bytes() == b"must survive"


def test_retention_apply_reports_actual_success(tmp_path: Path, capsys):
	root = tmp_path / "robot"
	root.mkdir()
	data_file = root / "delete-me.mcap"
	data_file.write_bytes(b"payload")
	_verified_old_upload(data_file)

	rc = main([
		"retention", "apply", "--root", str(root), "--min-retain-days", "0",
		"--max-total-gib", "0.000000001", "--execute",
	])
	out = json.loads(capsys.readouterr().out)

	assert rc == 0
	assert out["deleted"] == 1
	assert out["failed"] == []
	assert out["bytes_freed"] == len(b"payload")
	assert not data_file.exists()
	assert not manifest_path_for(data_file).exists()


def test_retention_apply_returns_nonzero_when_capacity_target_is_unmet(
	tmp_path: Path, capsys
):
	data_file = tmp_path / "protected.mcap"
	data_file.write_bytes(b"x" * 4096)
	old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
	os.utime(data_file, (old, old))
	manifest = load_or_create_manifest(data_file)
	manifest["status"] = "completed"  # deliberately lacks hash/upload evidence
	save_manifest(manifest)

	rc = main([
		"retention", "apply", "--root", str(tmp_path), "--min-retain-days", "0",
		"--max-total-gib", "0.000001", "--execute",
	])
	out = json.loads(capsys.readouterr().out)

	assert rc == 1
	assert out["deleted"] == 0
	assert out["capacity_target_met_after_apply"] is False
	assert data_file.exists()


def test_package_level_import_works():
	"""from robot_storage_manager import scan_for_data_files must not raise
	ImportError -- __init__.py's __all__ must match what it actually imports.
	"""
	from robot_storage_manager import (
		scan_for_data_files,
		load_or_create_manifest as pkg_load_or_create_manifest,
		save_manifest as pkg_save_manifest,
		compute_sha256_for_file,
		RetentionPlanner,
		is_file_stable as pkg_is_file_stable,
	)

	assert callable(scan_for_data_files)
	assert callable(pkg_load_or_create_manifest)
	assert callable(pkg_save_manifest)
	assert callable(compute_sha256_for_file)
	assert callable(pkg_is_file_stable)
	assert RetentionPlanner is not None
