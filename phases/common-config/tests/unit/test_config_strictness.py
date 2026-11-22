import json

import pytest

from robot_vault_config.model import load_config


def _load(tmp_path, value):
    path = tmp_path / "vault.json"
    if not isinstance(value, str):
        value = json.dumps({"robot_id": "rover-7", **value})
    path.write_text(value)
    return load_config(path)


@pytest.mark.parametrize(
    "value,match",
    [
        ({"serveer": {}}, "unknown top-level"),
        ({"server": {"prot": 50051}}, "unknown server"),
        ({"server": []}, "server must be a JSON object"),
        ({"server": {"port": 0}}, "server.port"),
        ({"server": {"port": 70000}}, "server.port"),
        ({"upload_agent": {"concurrency": 0}}, "upload_agent.concurrency"),
        ({"upload_agent": {"parallel_streams": 0}}, "upload_agent.parallel_streams"),
        ({"upload_agent": {"max_retries": -1}}, "upload_agent.max_retries"),
        ({"retention": {"max_total_gib": 0}}, "retention.max_total_gib"),
        ({"paths": {"robot_root": "relative"}}, "paths.robot_root"),
        ({"paths": {"robot_root": "/srv/recordings /etc"}}, "paths.robot_root"),
        ({"paths": {"robot_root": "/srv/recordings\t/etc"}}, "paths.robot_root"),
        ({"auth": {"token_file": "  "}}, "auth.token_file"),
        # The inline shared token is gone; per-robot token files replace it.
        ({"auth": {"token": "top-secret"}}, "unknown auth configuration key"),
        # One TLS switch: tls.enabled. server.tls is not a key any more.
        ({"server": {"tls": True}}, "unknown server configuration key"),
    ],
)
def test_invalid_or_unknown_configuration_fails_closed(tmp_path, value, match):
    with pytest.raises(ValueError, match=match):
        _load(tmp_path, value)


def test_duplicate_json_keys_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="unique keys"):
        _load(tmp_path, '{"server":{"host":"safe","host":"attacker"}}')
