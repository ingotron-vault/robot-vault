"""One `small_file_threshold_mb` default, asserted against the
gRPC per-message cap at config load.

`UploadSmall` sends the WHOLE file in a single gRPC message. Both ends cap a
message at 50 MiB, so a threshold above that routes 50-100 MB files onto a
path that physically cannot carry them. One constant defines the default, and
the load-time check rejects any threshold above the cap.
"""
import json

import pytest

from robot_vault_config.model import (
	DEFAULT_CHUNK_SIZE_BYTES,
	DEFAULT_SMALL_FILE_THRESHOLD_MB,
	MAX_CHUNK_SIZE_BYTES,
	MAX_GRPC_MESSAGE_BYTES,
	MAX_SMALL_FILE_THRESHOLD_MB,
	MIN_S3_PART_BYTES,
	UploadAgentSettings,
	load_config,
	validate_chunk_size_bytes,
	validate_small_file_threshold_mb,
)


def test_ceiling_leaves_headroom_under_the_grpc_message_cap():
	assert MAX_GRPC_MESSAGE_BYTES == 50 * 1024 * 1024
	assert MAX_SMALL_FILE_THRESHOLD_MB == 48
	assert (
		MAX_SMALL_FILE_THRESHOLD_MB * 1024 * 1024 < MAX_GRPC_MESSAGE_BYTES
	), "a small-path file plus protobuf framing must fit in one gRPC message"


def test_streaming_and_unary_uploads_have_distinct_ceiling_contracts():
	assert MIN_S3_PART_BYTES == 5 * 1024 * 1024
	assert DEFAULT_CHUNK_SIZE_BYTES == MIN_S3_PART_BYTES
	assert MAX_CHUNK_SIZE_BYTES == 8 * 1024 * 1024
	assert validate_chunk_size_bytes(MAX_CHUNK_SIZE_BYTES) == MAX_CHUNK_SIZE_BYTES
	with pytest.raises(ValueError, match="chunk_size_bytes"):
		validate_chunk_size_bytes(MAX_CHUNK_SIZE_BYTES + 1)
	assert MAX_SMALL_FILE_THRESHOLD_MB == 48


def test_there_is_exactly_one_default_and_it_is_under_the_ceiling():
	assert DEFAULT_SMALL_FILE_THRESHOLD_MB <= MAX_SMALL_FILE_THRESHOLD_MB
	assert UploadAgentSettings().small_file_threshold_mb == DEFAULT_SMALL_FILE_THRESHOLD_MB

	# UploadAgentConfig (phase 02) uses the same constant.
	from robot_upload_agent.core import UploadAgentConfig

	assert UploadAgentConfig().small_file_threshold_mb == DEFAULT_SMALL_FILE_THRESHOLD_MB


def test_validate_refuses_a_threshold_that_cannot_be_sent():
	"""An over-ceiling or negative threshold is a config error."""
	for bad in (100, MAX_SMALL_FILE_THRESHOLD_MB + 1, -1):
		with pytest.raises(ValueError, match="small_file_threshold_mb"):
			validate_small_file_threshold_mb(bad, "unit test")
	# The boundary itself is allowed.
	assert validate_small_file_threshold_mb(MAX_SMALL_FILE_THRESHOLD_MB) == (
		MAX_SMALL_FILE_THRESHOLD_MB
	)


def test_vault_config_load_refuses_an_oversized_threshold(tmp_path):
	cfg_path = tmp_path / "vault.json"
	cfg_path.write_text(json.dumps({"robot_id": "rover-7", "upload_agent": {"small_file_threshold_mb": 100}}))
	with pytest.raises(ValueError, match="small_file_threshold_mb"):
		load_config(cfg_path)


def test_vault_config_load_accepts_a_sane_threshold(tmp_path):
	cfg_path = tmp_path / "vault.json"
	cfg_path.write_text(json.dumps({"robot_id": "rover-7", "upload_agent": {"small_file_threshold_mb": 8}}))
	assert load_config(cfg_path).upload_agent.small_file_threshold_mb == 8


def test_shipped_configs_are_all_within_the_ceiling():
	"""Every committed vault config must load without tripping the guard."""
	from pathlib import Path

	repo_root = Path(__file__).resolve().parents[4]
	configs = sorted((repo_root / "configs").glob("vault*.json"))
	assert configs, "no configs/vault*.json found"
	for path in configs:
		cfg = load_config(path)
		assert cfg.upload_agent.small_file_threshold_mb <= MAX_SMALL_FILE_THRESHOLD_MB, path
