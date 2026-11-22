import json
import logging

import pytest


def test_token_file_wins_over_token_and_env(tmp_path, monkeypatch):
	"""Would break if resolve_auth_token's precedence order stopped putting
	auth.token_file ahead of the explicit token and INGEST_AUTH_TOKEN."""
	from robot_upload_agent.transport import resolve_auth_token

	monkeypatch.setenv("INGEST_AUTH_TOKEN", "from-env")
	token_file = tmp_path / "robot.token"
	token_file.write_text("from-file")

	assert resolve_auth_token("from-config", token_file=str(token_file)) == "from-file"


def test_token_wins_over_env_when_no_token_file(monkeypatch):
	"""Would break if adding the token_file tier accidentally demoted the explicit token
	below the INGEST_AUTH_TOKEN environment fallback."""
	from robot_upload_agent.transport import resolve_auth_token

	monkeypatch.setenv("INGEST_AUTH_TOKEN", "from-env")

	assert resolve_auth_token("from-config", token_file=None) == "from-config"


def test_env_used_when_neither_configured_none_when_nothing_set(monkeypatch):
	"""Would break if the INGEST_AUTH_TOKEN fallback tier were dropped, or if
	the "no credential at all" case stopped returning None."""
	from robot_upload_agent.transport import resolve_auth_token

	monkeypatch.setenv("INGEST_AUTH_TOKEN", "from-env")
	assert resolve_auth_token(None, token_file=None) == "from-env"

	monkeypatch.delenv("INGEST_AUTH_TOKEN", raising=False)
	assert resolve_auth_token(None, token_file=None) is None


def test_token_file_content_is_stripped(tmp_path):
	"""Would break if _read_token_file stopped stripping surrounding whitespace,
	e.g. shipping a trailing newline embedded inside the Bearer header."""
	from robot_upload_agent.transport import resolve_auth_token

	token_file = tmp_path / "robot.token"
	token_file.write_text("  \n ignr1.ABCDEF \n\t")

	assert resolve_auth_token(None, token_file=str(token_file)) == "ignr1.ABCDEF"


def test_missing_token_file_falls_through_with_warning(tmp_path, monkeypatch, caplog):
	"""Would break if a missing token_file raised instead of falling through to
	the next credential tier, or if the operator lost the diagnostic naming the
	bad path."""
	from robot_upload_agent.transport import resolve_auth_token

	monkeypatch.setenv("INGEST_AUTH_TOKEN", "from-env")
	missing = tmp_path / "does-not-exist"

	with caplog.at_level("WARNING"):
		result = resolve_auth_token(None, token_file=str(missing))

	assert result == "from-env"
	assert str(missing) in caplog.text


def test_empty_token_file_falls_through_to_next_tier(tmp_path, monkeypatch):
	"""Would break if a present-but-empty/whitespace-only token file were
	treated as a real (blank) credential instead of falling through to
	the explicit token."""
	from robot_upload_agent.transport import resolve_auth_token

	monkeypatch.setenv("INGEST_AUTH_TOKEN", "from-env")
	token_file = tmp_path / "robot.token"
	token_file.write_text("   \n\t")

	assert resolve_auth_token("from-config", token_file=str(token_file)) == "from-config"


def test_token_value_is_never_logged(tmp_path, caplog):
	"""Would break if resolve_auth_token (or _read_token_file) logged the
	credential's VALUE anywhere -- including at DEBUG -- instead of only the
	source name."""
	from robot_upload_agent.transport import resolve_auth_token

	sentinel = "ignr1.SENTINELTOKENVALUE0123456789ABCDEFGHJKMNPQ"
	token_file = tmp_path / "robot.token"
	token_file.write_text(sentinel)

	with caplog.at_level(logging.DEBUG):
		result = resolve_auth_token(None, token_file=str(token_file))

	assert result == sentinel
	assert "auth.token_file" in caplog.text
	assert sentinel not in caplog.text


def test_auth_token_file_round_trips_and_is_not_redacted(tmp_path):
	"""Would break if AuthSettings.token_file were removed from the loader
	schema, or if VaultConfig.to_dict() started redacting token_file as though
	a filesystem path were the secret."""
	from robot_vault_config.model import load_config

	token_path = str(tmp_path / "credentials" / "robot.token")
	config_path = tmp_path / "vault.json"
	config_path.write_text(json.dumps({
		"robot_id": "rover-7",
		"auth": {"token_file": token_path},
	}))

	cfg = load_config(config_path)
	assert cfg.auth.token_file == token_path

	rendered = cfg.to_dict()
	assert rendered["auth"] == {"token_file": token_path}


def test_unknown_auth_key_rejected_by_load_config(tmp_path):
	"""Would break if the loader stopped rejecting unknown auth keys -- the token
	file relies on this forward-only schema so an OLDER agent cannot silently
	ignore a token_file it does not understand."""
	from robot_vault_config.model import load_config

	config_path = tmp_path / "vault.json"
	config_path.write_text(json.dumps({
		"robot_id": "rover-7",
		"auth": {"tokenfile": "/etc/ingotron/credentials/robot.token"},
	}))

	with pytest.raises(ValueError, match="tokenfile"):
		load_config(config_path)


def test_retention_auto_apply_defaults_false_and_round_trips(tmp_path):
	"""Would break if RetentionSettings.auto_apply's default flipped to True
	(auto-deleting a customer's recordings by default) or if
	load_config stopped accepting an explicit override."""
	from robot_vault_config.model import RetentionSettings, load_config

	assert RetentionSettings().auto_apply is False

	config_path = tmp_path / "vault.json"
	config_path.write_text(json.dumps({"robot_id": "rover-7", "retention": {"auto_apply": True}}))

	cfg = load_config(config_path)
	assert cfg.retention.auto_apply is True


def test_config_validate_requires_credential_for_off_box_non_mtls_tls(tmp_path, monkeypatch):
	"""Would break if cmd_config_validate stopped requiring a credential for an
	off-box, non-mTLS TLS robot (the enrolled-robot case),
	or if auth.token_file stopped being accepted to satisfy it."""
	from robot_upload_agent.cli import main

	monkeypatch.delenv("INGEST_AUTH_TOKEN", raising=False)

	base_config = {
		"robot_id": "rover-7",
		"server": {"host": "vault.example.com", "port": 50051},
		"tls": {"enabled": True, "mtls": False, "ca_path": "/etc/ingotron/ca.crt"},
	}

	no_cred_path = tmp_path / "no_cred.json"
	no_cred_path.write_text(json.dumps(base_config))
	assert main(["config", "validate", "--config", str(no_cred_path)]) == 2

	token_file = tmp_path / "robot.token"
	token_file.write_text("ignr1.ABCDEF")
	with_cred = dict(base_config)
	with_cred["auth"] = {"token_file": str(token_file)}
	with_cred_path = tmp_path / "with_cred.json"
	with_cred_path.write_text(json.dumps(with_cred))
	assert main(["config", "validate", "--config", str(with_cred_path)]) == 0
