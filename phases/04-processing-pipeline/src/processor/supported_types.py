"""Supported ROS 2 message types -> normalized topic buckets.

The catalog itself lives in the shared library (`robot_vault_config.topics`),
so the query service imports the SAME map instead of keeping its own copy. This module stays as the processor's
import site -- `from processor.supported_types import ROS2_TYPE_NORMALIZATION`
still works everywhere it already did.

Add new message types in `phases/common-config/src/robot_vault_config/topics.py`,
then regenerate `phases/04-processing-pipeline/docs/supported_ros2_topics.yaml`:

    python -m robot_vault_config.topics > phases/04-processing-pipeline/docs/supported_ros2_topics.yaml
"""

from robot_vault_config.topics import (  # noqa: F401  (re-exported)
	CORE_ROS2_TYPES,
	LEGACY_OR_THIRD_PARTY_ROS2_TYPES,
	ROS2_TYPE_NORMALIZATION,
	normalize_topic,
)

__all__ = [
	"ROS2_TYPE_NORMALIZATION",
	"CORE_ROS2_TYPES",
	"LEGACY_OR_THIRD_PARTY_ROS2_TYPES",
	"normalize_topic",
]
