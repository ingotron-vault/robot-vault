"""Capture settings compatibility for partial and empty operator configs."""
import json
from pathlib import Path

from robot_vault_config.model import CaptureSettings, load_config

REPO_ROOT = Path(__file__).resolve().parents[4]
CAPTURE_COLUMNS = ("operator", "site", "permission_ref", "session_tags", "task_label")


def test_load_config_parses_a_partial_capture_section(tmp_path):
	"""An operator who only cares about `task_label` shouldn't have to spell
	out the other four as null -- CaptureSettings' own dataclass defaults
	(all None) fill the rest in, exactly like every other config section."""
	cfg_path = tmp_path / "vault.json"
	cfg_path.write_text(json.dumps({"robot_id": "rover-7", "capture": {"task_label": "inspection"}}))
	cfg = load_config(cfg_path)
	assert cfg.capture.task_label == "inspection"
	assert cfg.capture.operator is None
	assert cfg.capture.site is None
	assert cfg.capture.permission_ref is None
	assert cfg.capture.session_tags is None


def test_load_config_tolerates_an_explicit_empty_capture_section(tmp_path):
	cfg_path = tmp_path / "vault.json"
	cfg_path.write_text(json.dumps({"robot_id": "rover-7", "capture": {}}))
	cfg = load_config(cfg_path)
	assert cfg.capture == CaptureSettings()


def test_e2e_config_has_nonempty_capture_values():
	cfg = load_config(REPO_ROOT / "configs" / "vault.local.json")
	assert cfg.capture is not None
	assert all(getattr(cfg.capture, field) for field in CAPTURE_COLUMNS)
