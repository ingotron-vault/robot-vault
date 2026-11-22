"""Capture metadata: `load_or_create_manifest` picks up
`operator`/`site`/`permission_ref`/`session_tags`/`task_label` from
`robot_vault_config.model.VaultConfig.capture` at manifest-CREATION time, pins
them across rewrites the same way the manifest pins `chunk_size_bytes`, and defaults
every one of them to None for a manifest built without capture metadata (or
with no `cfg` at all).
"""
from pathlib import Path

from robot_vault_config.model import CaptureSettings, VaultConfig

from robot_storage_manager.core import (
	CAPTURE_MANIFEST_KEYS,
	load_or_create_manifest,
	save_manifest,
)


def _cfg_with_capture(**overrides) -> VaultConfig:
	return VaultConfig(capture=CaptureSettings(**overrides))


def test_new_manifest_picks_up_capture_fields_from_config(tmp_path: Path):
	data = tmp_path / "sample.mcap"
	data.write_bytes(b"hello world\n")
	cfg = _cfg_with_capture(
		operator="alice",
		site="lab-1",
		permission_ref="consent-42",
		session_tags="demo,indoor",
		task_label="pick_and_place",
	)

	mf = load_or_create_manifest(data, cfg=cfg)

	assert mf["operator"] == "alice"
	assert mf["site"] == "lab-1"
	assert mf["permission_ref"] == "consent-42"
	assert mf["session_tags"] == "demo,indoor"
	assert mf["task_label"] == "pick_and_place"


def test_new_manifest_without_cfg_defaults_capture_fields_to_none(tmp_path: Path):
	data = tmp_path / "sample.mcap"
	data.write_bytes(b"hello world\n")

	mf = load_or_create_manifest(data)

	for key in CAPTURE_MANIFEST_KEYS:
		assert mf[key] is None


def test_new_manifest_with_cfg_but_no_capture_section_defaults_to_none(tmp_path: Path):
	"""A VaultConfig loaded from a vault.json with no capture section has
	`cfg.capture is None` -- must behave identically to passing no cfg at
	all, not raise."""
	data = tmp_path / "sample.mcap"
	data.write_bytes(b"hello world\n")

	mf = load_or_create_manifest(data, cfg=VaultConfig())

	for key in CAPTURE_MANIFEST_KEYS:
		assert mf[key] is None


def test_capture_fields_are_preserved_across_a_reload_with_no_cfg(tmp_path: Path):
	"""Once written, capture context is pinned -- the same "pin at creation,
	never silently re-derive" discipline chunk_size_bytes uses. A
	later `load_or_create_manifest` call with no `cfg` (e.g. `rsm hash`,
	which doesn't currently pass one) must not wipe out what `rsm scan`
	already wrote."""
	data = tmp_path / "sample.mcap"
	data.write_bytes(b"hello world\n")
	cfg = _cfg_with_capture(operator="alice", task_label="pick_and_place")

	mf = load_or_create_manifest(data, cfg=cfg)
	save_manifest(mf)

	reloaded = load_or_create_manifest(data)  # no cfg this time
	assert reloaded["operator"] == "alice"
	assert reloaded["task_label"] == "pick_and_place"
	assert reloaded["site"] is None


def test_capture_fields_are_not_overwritten_by_a_later_different_config(tmp_path: Path):
	"""A mid-session config edit must not silently rewrite the session
	context a file was actually recorded under."""
	data = tmp_path / "sample.mcap"
	data.write_bytes(b"hello world\n")
	first_cfg = _cfg_with_capture(operator="alice")
	second_cfg = _cfg_with_capture(operator="bob")

	mf = load_or_create_manifest(data, cfg=first_cfg)
	save_manifest(mf)

	reloaded = load_or_create_manifest(data, cfg=second_cfg)
	assert reloaded["operator"] == "alice"
