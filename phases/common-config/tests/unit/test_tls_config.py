"""Fail-closed TLS policy for the robot upload client's shared config."""

import json
from pathlib import Path

import pytest

from robot_vault_config.model import (
	TlsSettings,
	VaultConfig,
	load_config,
)


REPO_ROOT = Path(__file__).resolve().parents[4]


def _load(tmp_path: Path, tls: dict | None = None) -> VaultConfig:
	data = {"robot_id": "rover-7", "server": {"host": "ingest.example", "port": 50051}}
	if tls is not None:
		data["tls"] = tls
	path = tmp_path / "vault.json"
	path.write_text(json.dumps(data))
	return load_config(path)


def test_tls_is_on_by_default_with_the_platform_trust_store(tmp_path):
	cfg = _load(tmp_path)
	assert cfg.tls == TlsSettings(enabled=True, mtls=False)
	assert _load(tmp_path, {}).tls.enabled is True


def test_explicit_plaintext_and_custom_ca_server_tls_are_valid(tmp_path):
	plain = _load(tmp_path, {"enabled": False, "mtls": False})
	assert plain.tls == TlsSettings(enabled=False, mtls=False)

	server_auth = _load(
		tmp_path,
		{"enabled": True, "mtls": False, "ca_path": "/etc/vault/ca.crt"},
	)
	assert server_auth.tls.ca_path == "/etc/vault/ca.crt"


def test_server_tls_is_not_a_second_switch(tmp_path):
	path = tmp_path / "vault.json"
	path.write_text(json.dumps({"robot_id": "rover-7", "server": {"tls": False}}))
	with pytest.raises(ValueError, match="unknown server configuration key"):
		load_config(path)


@pytest.mark.parametrize("missing", ["ca_path", "cert_path", "key_path"])
def test_mtls_requires_complete_ca_certificate_and_key_set(tmp_path, missing):
	tls = {
		"enabled": True,
		"mtls": True,
		"ca_path": "/etc/vault/ca.crt",
		"cert_path": "/etc/vault/robot.crt",
		"key_path": "/etc/vault/robot.key",
	}
	del tls[missing]
	with pytest.raises(ValueError, match=rf"tls\.{missing}"):
		_load(tmp_path, tls)


@pytest.mark.parametrize(
	"tls",
	[
		{"enabled": True, "mtls": False, "cert_path": "/robot.crt"},
		{"enabled": True, "mtls": False, "key_path": "/robot.key"},
	],
)
def test_client_certificate_and_key_are_atomic(tmp_path, tls):
	with pytest.raises(ValueError, match="must be configured together"):
		_load(tmp_path, tls)


def test_client_identity_is_rejected_when_mtls_is_false(tmp_path):
	with pytest.raises(ValueError, match=r"require tls\.mtls=true"):
		_load(
			tmp_path,
			{
				"enabled": True,
				"mtls": False,
				"cert_path": "/robot.crt",
				"key_path": "/robot.key",
			},
		)


@pytest.mark.parametrize(
	"tls",
	[
		{"enabled": False, "mtls": True},
		{"enabled": False, "mtls": False, "ca_path": "/ca.crt"},
	],
)
def test_plaintext_cannot_claim_mtls_or_carry_tls_material(tmp_path, tls):
	with pytest.raises(ValueError, match="requires|cannot be configured"):
		_load(tmp_path, tls)


def test_whitespace_only_mtls_path_is_missing(tmp_path):
	with pytest.raises(ValueError, match=r"tls\.key_path"):
		_load(
			tmp_path,
			{
				"enabled": True,
				"mtls": True,
				"ca_path": "/ca.crt",
				"cert_path": "/robot.crt",
				"key_path": "   ",
			},
		)


def test_directly_constructed_vault_config_is_validated():
	cfg = VaultConfig(tls=TlsSettings(enabled=False, mtls=True))
	with pytest.raises(ValueError, match=r"tls\.mtls=true requires tls\.enabled=true"):
		cfg.validate_tls()


def test_shipped_tls_and_remote_configs_are_complete_mtls():
	for name in ("vault.tls.local.json", "vault.remote.json"):
		cfg = load_config(REPO_ROOT / "configs" / name)
		assert cfg.tls.enabled and cfg.tls.mtls, name
		assert cfg.tls.ca_path and cfg.tls.cert_path and cfg.tls.key_path, name
