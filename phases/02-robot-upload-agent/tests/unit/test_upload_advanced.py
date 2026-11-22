"""
Additional unit tests for upload agent functionality: chunk-plan boundary
cases, upload-strategy selection, queue introspection, and manifest-recovery
edge cases not already covered by test_transport.py / test_queue_and_config.py
/ test_upload_hashing.py.

Covered elsewhere, so not repeated here:
  - Resume planning: without a negotiated resume session,
    compute_chunks_plan returns the full chunk set and ignores
    resume_bytes/missing_chunks (test_transport.py::test_resume_yields_full_plan);
    negotiated partial resume: test_partial_resume.py.
  - Retry: the in-process retry with backoff (core.py
    `_upload_file_with_retry`) is in test_retry_policy.py; the manual
    /api/upload/retry-failed endpoint uploads the failed files again.
  - Bandwidth throttling: test_queue_and_config.py::
    test_bandwidth_throttle_uses_queue_limiter.
  - Manifest creation, persistence and upload-id determinism: the phase-01
    manifest tests.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

from robot_upload_agent.core import (
	UploadAgentConfig,
	UploadQueue,
	get_network_type,
	check_server_connectivity,
)
from robot_upload_agent.transport import compute_chunks_plan
from robot_storage_manager.core import load_or_create_manifest, save_manifest


class TestChunkedUpload:
	"""Boundary-condition coverage for compute_chunks_plan on the non-resume path."""

	def test_chunk_size_calculation(self):
		chunk_size = 1024 * 1024  # 1MB chunks

		# File smaller than chunk size
		small_chunks = compute_chunks_plan(512 * 1024, chunk_size)
		assert len(small_chunks) == 1

		# File exactly chunk size
		exact_chunks = compute_chunks_plan(1024 * 1024, chunk_size)
		assert len(exact_chunks) == 1

		# File larger than chunk size
		large_chunks = compute_chunks_plan(3 * 1024 * 1024 + 512, chunk_size)
		assert len(large_chunks) == 4  # 3 full chunks + 1 partial


class TestUploadStrategies:
	"""Test that _upload_file picks the small vs. streaming path based on
	small_file_threshold_mb, at the unit level (direct call, mocked transport).
	"""

	def test_small_file_strategy(self, tmp_path):
		small_file = tmp_path / "small.mcap"
		small_file.write_bytes(b"x" * 1024)  # 1KB

		config = UploadAgentConfig(
			small_file_threshold_mb=5,
			chunk_size_bytes=8 * 1024 * 1024
		)

		transport = MagicMock()
		transport.upload_small = MagicMock(return_value={"status": "completed"})
		transport.upload_stream = MagicMock(return_value={"status": "completed"})

		queue = UploadQueue(root=tmp_path, config=config, transport=transport)
		manifest = load_or_create_manifest(small_file)
		queue._upload_file(small_file, manifest)

		transport.upload_small.assert_called_once()
		transport.upload_stream.assert_not_called()

	def test_large_file_strategy(self, tmp_path):
		large_file = tmp_path / "large.mcap"
		large_file.write_bytes(b"x" * 10 * 1024 * 1024)  # 10MB

		config = UploadAgentConfig(
			small_file_threshold_mb=5,
			chunk_size_bytes=5 * 1024 * 1024
		)

		transport = MagicMock()
		transport.upload_small = MagicMock(return_value={"status": "completed"})
		transport.upload_stream = MagicMock(return_value={"status": "completed"})

		queue = UploadQueue(root=tmp_path, config=config, transport=transport)
		manifest = load_or_create_manifest(large_file)
		queue._upload_file(large_file, manifest)

		transport.upload_stream.assert_called_once()
		transport.upload_small.assert_not_called()


class TestQueueIntrospection:
	"""Test queue.describe()/list_files() reporting, independent of the
	pending_count-only assertion in test_queue_and_config.py.
	"""

	def test_queue_describe(self, tmp_path):
		for i in range(3):
			f = tmp_path / f"file{i}.mcap"
			f.write_bytes(b"x" * 100)

		config = UploadAgentConfig()

		with patch('robot_upload_agent.core.get_network_type') as mock_net:
			mock_net.return_value = {
				"type": "ethernet",
				"interface": "eth0",
				"is_wifi": False,
				"is_cellular": False,
				"is_ethernet": True
			}

			with patch('robot_upload_agent.core.check_server_connectivity') as mock_conn:
				mock_conn.return_value = True

				queue = UploadQueue(root=tmp_path, config=config)
				desc = queue.describe()

		assert desc["pending_count"] == 3
		assert desc["connected"] is True
		assert desc["server_reachable"] is True

	def test_list_files(self, tmp_path):
		pending_file = tmp_path / "pending.mcap"
		pending_file.write_bytes(b"pending")

		uploaded_file = tmp_path / "uploaded.mcap"
		uploaded_file.write_bytes(b"uploaded")
		manifest = load_or_create_manifest(uploaded_file)
		manifest["status"] = "completed"
		save_manifest(manifest)

		config = UploadAgentConfig()
		queue = UploadQueue(root=tmp_path, config=config)

		files = queue.list_files()

		assert len(files) == 2
		statuses = {f["name"]: f["status"] for f in files}
		assert statuses["pending.mcap"] == "pending"
		assert statuses["uploaded.mcap"] == "completed"


class TestNetworkOperations:
	"""Test network-related helper functions directly."""

	def test_get_network_type_structure(self):
		network_info = get_network_type()

		assert "type" in network_info
		assert "interface" in network_info
		assert "is_wifi" in network_info
		assert "is_cellular" in network_info
		assert "is_ethernet" in network_info
		assert network_info["type"] in ["unknown", "wifi", "ethernet", "cellular"]

	def test_check_server_connectivity_rejects_closed_port(self):
		# Port 1 requires privileges to bind and nothing listens there, so a
		# connect attempt should fail deterministically.
		result = check_server_connectivity("127.0.0.1", 1, timeout=1)
		assert result is False


class TestManifestRecovery:
	"""Manifest-corruption recovery: unique coverage of the
	load_or_create_manifest branch that catches a JSON parse failure and
	regenerates a fresh pending manifest (preserving a .corrupt copy).
	"""

	def test_corrupted_manifest_recovery(self, tmp_path):
		test_file = tmp_path / "test.mcap"
		test_file.write_bytes(b"content")

		manifest_path = tmp_path / "test.mcap.manifest.json"
		manifest_path.write_text("{ corrupted json }")

		manifest = load_or_create_manifest(test_file)
		assert manifest["status"] == "pending"
		assert manifest["file_size_bytes"] == 7
