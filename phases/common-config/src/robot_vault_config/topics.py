"""Canonical ROS 2 topic normalization.

One map, one `normalize_topic()`, imported by every phase that has to turn a
`(schema_name, raw_topic)` pair into the stable bucket name used as the
`topic` column in Parquet, in `processed_topics`, and in every query API
path segment.

The processor (`phases/04-processing-pipeline/src/processor/supported_types.py`
and `processor.core`) and the query service
(`phases/05-query-service/src/query_service/uploads.py`) must use this one map.
A second copy would drift: if the query service mapped `sensor_msgs/msg/Imu` to
`"imu"` while the processor stores `"imu/linear_accel"`, the query service
would fail to match its own `expected_topics` and silently report 0 raw bytes
for every IMU topic.

`phases/04-processing-pipeline/docs/supported_ros2_topics.yaml` is *generated*
from this module (`render_supported_types_yaml()`, checked by
`phases/04-processing-pipeline/tests/unit/test_processing_core.py`) so it
cannot drift.

Dependency-free on purpose: the processor image, the query image and the dev
image all install `phases/common-config`, and none of them should need a YAML
parser to normalize a topic name.
"""
from __future__ import annotations

from typing import Dict, Optional

# ---------------------------------------------------------------------------
# The catalog. Split into two ordered blocks purely so the generated YAML can
# reproduce the same two sections in the generated file.
# ---------------------------------------------------------------------------

#: Core sensor/geometry/nav types from standard ROS 2 distributions.
CORE_ROS2_TYPES: Dict[str, str] = {
	"sensor_msgs/msg/Imu": "imu",  # further split by raw topic hints
	"sensor_msgs/msg/NavSatFix": "gps/fix",
	"sensor_msgs/msg/NavSatStatus": "gps/status",
	"diagnostic_msgs/msg/DiagnosticArray": "diagnostics",
	"geometry_msgs/msg/Twist": "twist",
	"nav_msgs/msg/Odometry": "odom",
	"geometry_msgs/msg/PoseWithCovarianceStamped": "pose",
	"sensor_msgs/msg/BatteryState": "battery",
	"sensor_msgs/msg/Temperature": "temperature",
	"sensor_msgs/msg/JointState": "joint_state",
	"tf2_msgs/msg/TFMessage": "tf",
	"sensor_msgs/msg/LaserScan": "laser_scan",
	"sensor_msgs/msg/PointCloud2": "pointcloud2",
	"sensor_msgs/msg/Image": "image",
	"sensor_msgs/msg/CompressedImage": "compressed_image",
	"std_msgs/msg/Bool": "bool",
	"geometry_msgs/msg/PoseStamped": "pose",
	"nav_msgs/msg/Path": "path",
	"sensor_msgs/msg/Range": "range",
	"sensor_msgs/msg/MagneticField": "magnetic_field",
	"sensor_msgs/msg/FluidPressure": "fluid_pressure",
	"std_msgs/msg/Float32": "float32",
	"std_msgs/msg/Int32": "int32",
	"geometry_msgs/msg/AccelWithCovarianceStamped": "accel",
	"sensor_msgs/msg/Joy": "joy",
	"nav_msgs/msg/OccupancyGrid": "occupancy_grid",
	"sensor_msgs/msg/Illuminance": "illuminance",
	"sensor_msgs/msg/RelativeHumidity": "relative_humidity",
	"sensor_msgs/msg/TimeReference": "time_reference",
	"std_msgs/msg/String": "string",
	"visualization_msgs/msg/MarkerArray": "marker_array",
	"geometry_msgs/msg/PolygonStamped": "polygon",
	"nav_msgs/msg/MapMetaData": "map_metadata",
	"map_msgs/msg/OccupancyGridUpdate": "map/occupancy_grid_update",
	"rosgraph_msgs/msg/Clock": "clock",
	"rcl_interfaces/msg/Log": "log",
	"lifecycle_msgs/msg/State": "lifecycle_state",
	"control_msgs/msg/JointTrajectoryControllerState": "joint_trajectory_controller_state",
	"controller_manager_msgs/msg/ControllerState": "controller_state",
	"sensor_msgs/msg/CameraInfo": "camera_info",
	"stereo_msgs/msg/DisparityImage": "disparity_image",
	"geographic_msgs/msg/GeoPointStamped": "geo_point_stamped",
	"nmea_msgs/msg/Sentence": "nmea_sentence",
	"ackermann_msgs/msg/AckermannDriveStamped": "ackermann_drive",
	"radar_msgs/msg/RadarScan": "radar_scan",
	"sensor_msgs/msg/MultiEchoLaserScan": "multi_echo_laser_scan",
	"sensor_msgs/msg/PointCloud": "pointcloud",
	"trajectory_msgs/msg/JointTrajectory": "joint_trajectory",
	"trajectory_msgs/msg/MultiDOFJointTrajectory": "multi_dof_joint_trajectory",
}

#: Likely ROS1-only or third-party (kept for detection but may not appear in
#: ROS 2 bags).
LEGACY_OR_THIRD_PARTY_ROS2_TYPES: Dict[str, str] = {
	"actionlib_msgs/msg/GoalStatusArray": "ros1_action_status",
	"move_base_msgs/msg/MoveBaseActionGoal": "ros1_move_base_goal",
	"move_base_msgs/msg/MoveBaseActionResult": "ros1_move_base_result",
	"mavros_msgs/msg/State": "mavros/state",
	"costmap_2d_msgs/msg/CostmapUpdate": "costmap_update",
	"bond/msg/Status": "bond_status",
	"dbw_msgs/msg/BrakeCmd": "dbw/brake_cmd",
	"dbw_msgs/msg/ThrottleCmd": "dbw/throttle_cmd",
	"dbw_msgs/msg/SteeringCmd": "dbw/steering_cmd",
	"can_msgs/msg/Frame": "can_frame",
	"wireless_msgs/msg/Connection": "wireless_connection",
	"safety_msgs/msg/EmergencyState": "safety/emergency_state",
	"health_msgs/msg/SystemStatus": "health/system_status",
	"power_msgs/msg/PowerReport": "power/report",
	# Note: sensor_msgs/msg/PointField is a sub-message, not a typical topic payload
}

#: The full schema-name -> normalized-bucket catalog. Kept under its original
#: name so `from processor.supported_types import ROS2_TYPE_NORMALIZATION`
#: (which now re-exports this) and every existing test keep working.
ROS2_TYPE_NORMALIZATION: Dict[str, str] = {
	**CORE_ROS2_TYPES,
	**LEGACY_OR_THIRD_PARTY_ROS2_TYPES,
}

#: Raw-topic substrings that specialize an IMU message into its two charting
#: buckets. Applied only to `sensor_msgs/msg/Imu`.
_IMU_ANGULAR_HINTS = ("ang", "gyro")
_IMU_LINEAR_HINTS = ("lin", "acc")


def normalize_topic(schema_name: Optional[str], raw_topic: Optional[str]) -> str:
	"""Map a `(schema_name, raw_topic)` pair to a stable, type-based bucket.

	Rules, in order:

	1. exact `ROS2_TYPE_NORMALIZATION` hit -- with IMU specialized into
	   `imu/angular_vel` or `imu/linear_accel` when the raw topic name hints
	   at which half it carries (charting convenience; a bare `/imu` topic
	   stays `imu`);
	2. loose substring fallbacks for Imu/Image/PointCloud2 schemas that are
	   spelled slightly differently by non-standard producers;
	3. otherwise the raw topic, unchanged.

	This is the function that decides the `topic` value written into Parquet
	and `processed_topics`, so any reader that wants to match those rows --
	the query service included -- must call exactly this, not a local copy.
	"""
	normalized = ROS2_TYPE_NORMALIZATION.get(schema_name or "")
	if normalized:
		if (schema_name or "").endswith("/Imu"):
			tname_hint = (raw_topic or "").lower()
			if any(k in tname_hint for k in _IMU_ANGULAR_HINTS):
				return "imu/angular_vel"
			if any(k in tname_hint for k in _IMU_LINEAR_HINTS):
				return "imu/linear_accel"
		return normalized

	sname = (schema_name or "").lower()
	tname = (raw_topic or "").lower()
	# sensor_msgs/msg/Imu
	if "imu" in sname and ("sensor_msgs" in sname or "msg/imu" in sname):
		if any(k in tname for k in _IMU_ANGULAR_HINTS):
			return "imu/angular_vel"
		if any(k in tname for k in _IMU_LINEAR_HINTS):
			return "imu/linear_accel"
		return "imu"
	# sensor_msgs/msg/Image
	if ("image" in sname) and ("sensor_msgs" in sname or "msg/image" in sname):
		return "image"
	# sensor_msgs/msg/PointCloud2
	if ("pointcloud2" in sname) and ("sensor_msgs" in sname or "msg/pointcloud2" in sname):
		return "pointcloud2"
	return raw_topic or ""


def render_supported_types_yaml() -> str:
	"""Render `phases/04-processing-pipeline/docs/supported_ros2_topics.yaml`.

	The docs file is a *generated artifact* of this module. Regenerate with:

	    python -m robot_vault_config.topics > \\
	        phases/04-processing-pipeline/docs/supported_ros2_topics.yaml

	A unit test compares the checked-in file against this output, so the
	catalog and its documentation cannot drift apart.
	"""
	lines = [
		"# Canonical ROS 2 message types supported by the processor and their normalized buckets.",
		"# Many come from standard ROS 2 distributions (sensor_msgs, geometry_msgs, nav_msgs, etc.).",
		"# Some entries are ROS1 or third-party and may not appear in ROS 2 bags; kept for reference.",
		"",
	]
	lines += [f"{schema}: {bucket}" for schema, bucket in CORE_ROS2_TYPES.items()]
	lines += ["", "# Likely ROS1-only or non-core packages:"]
	lines += [
		f"{schema}: {bucket}" for schema, bucket in LEGACY_OR_THIRD_PARTY_ROS2_TYPES.items()
	]
	lines += [
		"",
		"# Note: sensor_msgs/msg/PointField is a sub-message, not a typical topic payload",
		"",
	]
	return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - tiny CLI for regeneration
	import sys

	sys.stdout.write(render_supported_types_yaml())
