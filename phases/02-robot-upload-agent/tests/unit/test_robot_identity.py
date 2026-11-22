"""Robot identity is declared config, never a guess about the file path.

`vault.json` carries a required top-level `robot_id`, threaded
`VaultConfig -> UploadAgentConfig -> GrpcUploadTransport -> UploadMetadata`.
There is no path heuristic: a config without `robot_id` does not load, and a
transport without one refuses to build upload metadata.
"""
import json

import pytest

from robot_vault_config.model import load_config
from robot_upload_agent.core import UploadAgentConfig
from robot_upload_agent.transport import require_robot_id


def test_configured_id_is_trimmed():
    assert require_robot_id("  rover-7\n") == "rover-7"


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_a_missing_id_is_refused(configured):
    with pytest.raises(ValueError, match="robot_id"):
        require_robot_id(configured)


# --- config plumbing --------------------------------------------------------


def _write_config(tmp_path, **extra):
    cfg = {
        "paths": {"robot_root": "/robot_data", "extension": ".mcap"},
        "server": {"host": "ingest", "port": 50051},
        "tls": {"enabled": False},
    }
    cfg.update(extra)
    path = tmp_path / "vault.json"
    path.write_text(json.dumps(cfg))
    return path


def test_vault_config_reads_robot_id(tmp_path):
    cfg = load_config(_write_config(tmp_path, robot_id="rover-7"))
    assert cfg.robot_id == "rover-7"


@pytest.mark.parametrize("extra", [{}, {"robot_id": "   "}, {"robot_id": None}])
def test_vault_config_without_robot_id_is_refused(tmp_path, extra):
    with pytest.raises(ValueError, match="robot_id is required"):
        load_config(_write_config(tmp_path, **extra))


def test_upload_agent_config_carries_the_id_from_the_vault_config(tmp_path):
    """The value has to survive the VaultConfig -> UploadAgentConfig hop,
    which is what `UploadQueue` builds the transport from."""
    agent_cfg = UploadAgentConfig.from_vault(load_config(_write_config(tmp_path, robot_id="rover-7")))
    assert agent_cfg.robot_id == "rover-7"


# --- the metadata actually sent --------------------------------------------


def test_transport_sends_the_configured_id_on_the_upload_metadata():
    """End of the chain: what lands in `UploadMetadata.robot_id`, which is
    what the ingest server checks against the `robots` table."""
    pb = pytest.importorskip(
        "upload_pb2",
        reason="generated protobuf stubs are only present after `make proto`",
    )
    from robot_upload_agent import transport as transport_mod

    manifest = {"path": "/robot_data/demo/2024-01-01/a.mcap", "file_size_bytes": 3}

    md = transport_mod._metadata_from_manifest(manifest, "rover-7")
    assert md.robot_id == "rover-7"
    assert isinstance(md, pb.UploadMetadata)

    # No configured id: refused, never derived from the path.
    with pytest.raises(ValueError, match="robot_id"):
        transport_mod._metadata_from_manifest(manifest, None)
