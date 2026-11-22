"""One topic-normalization module, imported by every phase.

Guards the two failure modes that motivated the merge:
  1. the query service's inline 12-entry map silently zeroed IMU/GPS stats
     because it flattened `sensor_msgs/msg/Imu` to `"imu"` while the processor
     writes `"imu/linear_accel"`;
  2. `docs/supported_ros2_topics.yaml` drifted because nothing generated or
     checked it.
"""
from pathlib import Path

from robot_vault_config.topics import (
	CORE_ROS2_TYPES,
	LEGACY_OR_THIRD_PARTY_ROS2_TYPES,
	ROS2_TYPE_NORMALIZATION,
	normalize_topic,
	render_supported_types_yaml,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SUPPORTED_TYPES_YAML = (
	REPO_ROOT / "phases" / "04-processing-pipeline" / "docs" / "supported_ros2_topics.yaml"
)


def test_catalog_is_the_full_processor_map():
	assert len(ROS2_TYPE_NORMALIZATION) == len(CORE_ROS2_TYPES) + len(
		LEGACY_OR_THIRD_PARTY_ROS2_TYPES
	)
	# The entries the drifted query-service copy was missing entirely.
	assert ROS2_TYPE_NORMALIZATION["sensor_msgs/msg/NavSatFix"] == "gps/fix"
	assert ROS2_TYPE_NORMALIZATION["sensor_msgs/msg/NavSatStatus"] == "gps/status"
	assert ROS2_TYPE_NORMALIZATION["sensor_msgs/msg/BatteryState"] == "battery"


def test_imu_is_specialized_by_raw_topic_hint():
	"""This is the exact mismatch that zeroed the query service's IMU stats.

	The processor writes `imu/linear_accel` into `processed_topics`; the query
	service compared against a flat `"imu"` from its own copy of the map, so
	`normalized_topic in expected_topics` was never true and every IMU raw-byte
	figure came back 0.
	"""
	assert normalize_topic("sensor_msgs/msg/Imu", "/imu/linear_accel") == "imu/linear_accel"
	assert normalize_topic("sensor_msgs/msg/Imu", "/imu/angular_vel") == "imu/angular_vel"
	assert normalize_topic("sensor_msgs/msg/Imu", "/gyro") == "imu/angular_vel"
	# A bare IMU topic with no hint keeps the plain bucket.
	assert normalize_topic("sensor_msgs/msg/Imu", "/imu") == "imu"


def test_unknown_schema_falls_through_to_the_raw_topic():
	assert normalize_topic("my_pkg/msg/Widget", "/widgets") == "/widgets"
	assert normalize_topic(None, None) == ""


def test_loose_schema_fallbacks_still_work():
	assert normalize_topic("Sensor_Msgs/msg/Image_raw", "/cam") == "image"
	assert normalize_topic("sensor_msgs/msg/pointcloud2", "/pc") == "pointcloud2"


def test_processor_and_query_service_import_the_same_function():
	from processor.supported_types import ROS2_TYPE_NORMALIZATION as processor_map
	from processor.supported_types import normalize_topic as processor_normalize

	assert processor_map is ROS2_TYPE_NORMALIZATION
	assert processor_normalize is normalize_topic


def test_docs_yaml_is_generated_from_this_module():
	"""The YAML is an artifact, not a second source of truth.

	Regenerate with:
	    python -m robot_vault_config.topics > \\
	        phases/04-processing-pipeline/docs/supported_ros2_topics.yaml
	"""
	assert SUPPORTED_TYPES_YAML.read_text() == render_supported_types_yaml()
