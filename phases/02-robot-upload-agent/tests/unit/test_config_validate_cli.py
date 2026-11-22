import copy
import json

import pytest

from robot_upload_agent.cli import main


# Same section shape as a generated off-box robot config. In particular, auth is omitted while
# release mTLS and the environment-based ingest credential are enabled.
SMOKE_CONFIG = {
	"robot_id": "robot-01",
	"paths": {
		"robot_root": "/robot/data",
		"extension": ".mcap",
	},
	"retention": {"min_retain_days": 14, "max_total_gib": 10},
	"upload_agent": {
		"concurrency": 1,
		"bandwidth_mbps": 0,
		"chunk_size_bytes": 5242880,
		"max_retries": 3,
		"base_backoff_seconds": 1.0,
		"parallel_streams": 1,
		"small_file_threshold_mb": 4,
		"compute_chunk_hashes": True,
		"min_stable_age_seconds": 5,
	},
	"server": {
		"host": "vault.example.test",
		"port": 50051,
	},
	"tls": {
		"enabled": True,
		"mtls": True,
		"ca_path": "/robot-pki/ca.crt",
		"cert_path": "/robot-pki/robot-01.crt",
		"key_path": "/robot-pki/robot-01.key",
	},
	"capture": {
		"operator": "operator-1",
		"site": "lab-1",
		"permission_ref": "consent-42",
		"session_tags": "demo",
		"task_label": "demo_task",
	},
}


def _write_config(tmp_path, config):
	path = tmp_path / "vault.json"
	path.write_text(json.dumps(config))
	return path


def _validate(path):
	return main(["config", "validate", "--config", str(path)])


def test_offbox_config_uses_environment_credential_without_traceback(
	tmp_path, monkeypatch, capsys
):
	monkeypatch.setenv("INGEST_AUTH_TOKEN", "test-ingest-token")

	assert _validate(_write_config(tmp_path, SMOKE_CONFIG)) == 0

	captured = capsys.readouterr()
	assert captured.err == ""
	output = json.loads(captured.out)
	assert output["auth"] is None


@pytest.mark.parametrize("explicit_null", [False, True], ids=["omitted", "null"])
def test_off_box_config_without_ingest_credential_fails_closed(
	tmp_path, monkeypatch, capsys, explicit_null
):
	config = copy.deepcopy(SMOKE_CONFIG)
	if explicit_null:
		config["auth"] = None
	monkeypatch.delenv("INGEST_AUTH_TOKEN", raising=False)

	assert _validate(_write_config(tmp_path, config)) == 2

	captured = capsys.readouterr()
	assert captured.out == ""
	error = json.loads(captured.err)
	# The requirement is "TLS is on and the server is off-box", not "mTLS is
	# on": an ENROLLED robot runs mtls=false and presents a per-robot `ignr1.`
	# token, so an mTLS-only rule would let it validate clean with no
	# credential. auth.token_file is one of the two ways to satisfy it.
	assert error == {
		"error": (
			"a credential is required for an off-box TLS robot; "
			"set auth.token_file (the enrolled path) or INGEST_AUTH_TOKEN"
		),
		"command": "config validate",
	}
	assert "Traceback" not in captured.err


def test_inline_auth_token_is_rejected(tmp_path, monkeypatch, capsys):
	"""The shared inline token is gone: per-robot token files replace it."""
	config = copy.deepcopy(SMOKE_CONFIG)
	config["auth"] = {"token": "config-token"}
	monkeypatch.delenv("INGEST_AUTH_TOKEN", raising=False)

	assert _validate(_write_config(tmp_path, config)) == 2

	captured = capsys.readouterr()
	assert captured.out == ""
	assert "unknown auth configuration key(s): token" in captured.err
	assert "config-token" not in captured.err
