"""Shared cross-phase vocabulary and configuration for Robot Vault.

Installed into every image that needs it (root `Dockerfile` for phases 01/02
and the dev/test container, `phases/04-processing-pipeline/Dockerfile`, and
`phases/05-query-service/Dockerfile`) so a single definition really does reach
every service rather than being copy-pasted per phase.

Contents:
  * `model`          -- the vault config dataclasses + `load_config`
  * `edition`        -- the canonical core/pro deployment vocabulary; import it
                        as `robot_vault_config.edition` (not re-exported here, so
                        `python -m robot_vault_config.edition` runs without a
                        runpy warning)
  * `upload_status`  -- the one canonical `UploadStatus` vocabulary
  * `episode_status` -- the one canonical `EpisodeStatus` vocabulary
  * `topics`         -- the one canonical ROS 2 topic normalization
"""

from .episode_status import (
	PERSISTED_EPISODE_STATUSES,
	EpisodeStatus,
)
from .episode_status import is_terminal as episode_status_is_terminal
from .episode_status import parse as parse_episode_status
from .episode_status import values as episode_status_values
from .model import (
	DEFAULT_CHUNK_SIZE_BYTES,
	DEFAULT_PARALLEL_STREAMS,
	DEFAULT_SMALL_FILE_THRESHOLD_MB,
	MAX_CHUNK_SIZE_BYTES,
	MAX_GRPC_MESSAGE_BYTES,
	MAX_PARALLEL_STREAMS,
	MAX_SMALL_FILE_THRESHOLD_MB,
	MIN_S3_PART_BYTES,
	VaultConfig,
	load_config,
	validate_chunk_size_bytes,
	validate_small_file_threshold_mb,
)
from .topics import (
	ROS2_TYPE_NORMALIZATION,
	normalize_topic,
	render_supported_types_yaml,
)
from .upload_status import (
	PERSISTED_UPLOAD_STATUSES,
	UploadStatus,
	is_success,
	is_terminal,
	parse,
)

__all__ = [
	"VaultConfig",
	"load_config",
	"DEFAULT_CHUNK_SIZE_BYTES",
	"DEFAULT_PARALLEL_STREAMS",
	"DEFAULT_SMALL_FILE_THRESHOLD_MB",
	"MAX_CHUNK_SIZE_BYTES",
	"MAX_GRPC_MESSAGE_BYTES",
	"MAX_PARALLEL_STREAMS",
	"MAX_SMALL_FILE_THRESHOLD_MB",
	"MIN_S3_PART_BYTES",
	"validate_chunk_size_bytes",
	"validate_small_file_threshold_mb",
	"ROS2_TYPE_NORMALIZATION",
	"normalize_topic",
	"render_supported_types_yaml",
	"UploadStatus",
	"PERSISTED_UPLOAD_STATUSES",
	"is_success",
	"is_terminal",
	"parse",
	# EpisodeStatus. `parse`/`is_terminal` are already taken by
	# UploadStatus above, so the episode-status helpers are exported under
	# names that name the vocabulary they act on rather than shadowing the
	# upload ones.
	"EpisodeStatus",
	"PERSISTED_EPISODE_STATUSES",
	"parse_episode_status",
	"episode_status_is_terminal",
	"episode_status_values",
]
