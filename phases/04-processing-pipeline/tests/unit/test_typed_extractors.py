"""Unit tests for the v2 typed extractors.

`extractors.extract_xyz` answers "what are the three numbers to plot?" for
every catalogued type. `typed_extractors` answers "what does this message
say?" for the six AMR-relevant ones, and returns one row per record inside
the message -- per joint, per transform -- which is the shape the v2 schema
families are built around.

Three decode paths are exercised here, deliberately:

  1. the JSON fallback (`message_encoding == "json"`), end to end through
     the public `extract_typed_rows` -- this needs no ROS 2 runtime, so it
     runs everywhere including the dev container the five-dir gate uses;
  2. the ROS-2-object path (`_rows_from_ros2`) driven with plain attribute
     objects. The functions under test navigate a decoded message purely
     through `cdr_decode.get_attr`, so a `SimpleNamespace` shaped like a
     `JointState` exercises the same field walking a real one would --
     without pretending to test CDR deserialization itself;
  3. schema-bearing CDR round-trips (`rosbags` Humble serializer -> the
     official `mcap_ros2.decoder.DecoderFactory`), which run unconditionally
     in the normal Python test environment. The decoder receives the exact
     embedded `ros2msg` definition, so the test covers the same no-rclpy path
     the shipped image uses and cannot pass by returning None.
"""
import base64
import json
import math
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from mcap.records import Schema
from rosbags.typesys.stores.ros2_humble import (
    builtin_interfaces__msg__Time as Time,
    sensor_msgs__msg__PointCloud2 as PointCloud2,
    sensor_msgs__msg__PointField as PointField,
    std_msgs__msg__Header as Header,
)

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(ROOT / "development"))

from sim_gen import messages as sim_messages  # noqa: E402
from processor.cdr_decode import McapRos2Decoder  # noqa: E402
from processor import typed_extractors as tx  # noqa: E402
from processor.typed_extractors import (  # noqa: E402
    extract_typed_rows,
    family_for_schema,
    is_static_tf_topic,
)


def _json(schema: str, topic: str, payload: dict):
    return extract_typed_rows(schema, topic, "json", json.dumps(payload).encode())


# --- family routing ---------------------------------------------------------

class TestFamilyRouting:
    def test_the_six_typed_types_map_to_their_families(self):
        assert family_for_schema("sensor_msgs/msg/JointState") == "joint_state"
        assert family_for_schema("nav_msgs/msg/Odometry") == "odometry"
        assert family_for_schema("geometry_msgs/msg/Twist") == "twist"
        assert family_for_schema("tf2_msgs/msg/TFMessage") == "tf"
        assert family_for_schema("sensor_msgs/msg/CameraInfo") == "camera_info"
        assert family_for_schema("sensor_msgs/msg/PointCloud2") == "pointcloud_stats"

    def test_everything_else_has_no_typed_family(self):
        """Most of the catalog is v1-only, and must stay that way: a type
        with no typed family writes no v2 file at all."""
        for schema in ("sensor_msgs/msg/Imu", "sensor_msgs/msg/BatteryState", "", None):
            assert family_for_schema(schema) is None
            assert extract_typed_rows(schema or "", "/t", "json", b"{}") == []

    def test_unknown_encoding_yields_no_rows(self):
        """Neither CDR nor JSON: nothing to decode, and inventing a row
        would put a message with no readable content in the lake."""
        assert extract_typed_rows(
            "nav_msgs/msg/Odometry", "/odom", "protobuf", b"\x00\x01"
        ) == []

    def test_unparseable_json_yields_no_rows(self):
        assert extract_typed_rows(
            "nav_msgs/msg/Odometry", "/odom", "json", b"not json at all"
        ) == []


# --- joint_state ------------------------------------------------------------

class TestJointState:
    def test_one_row_per_joint(self):
        rows = _json(
            "sensor_msgs/msg/JointState",
            "/joint_states",
            {
                "name": ["left_wheel", "right_wheel"],
                "position": [1.5, -2.5],
                "velocity": [0.25, -0.25],
                "effort": [10.0, 11.0],
            },
        )
        assert rows == [
            {"joint_name": "left_wheel", "position": 1.5, "velocity": 0.25, "effort": 10.0},
            {"joint_name": "right_wheel", "position": -2.5, "velocity": -0.25, "effort": 11.0},
        ]

    def test_absent_arrays_are_null_not_zero(self):
        """A position-only controller is normal. Those joints have unknown
        velocity/effort -- NULL -- and calling them 0.0 would be a
        fabricated measurement in a training set."""
        rows = _json(
            "sensor_msgs/msg/JointState",
            "/joint_states",
            {"name": ["j0"], "position": [0.75]},
        )
        assert rows == [
            {"joint_name": "j0", "position": 0.75, "velocity": None, "effort": None}
        ]

    def test_short_name_array_still_yields_the_numbers(self):
        """`name` is not required by the message definition. Dropping the
        data would be silent loss; a positional label keeps it."""
        rows = _json(
            "sensor_msgs/msg/JointState",
            "/joint_states",
            {"name": ["j0"], "position": [1.0, 2.0]},
        )
        assert [r["joint_name"] for r in rows] == ["j0", "joint_1"]
        assert [r["position"] for r in rows] == [1.0, 2.0]

    def test_empty_message_yields_no_rows(self):
        assert _json("sensor_msgs/msg/JointState", "/joint_states", {"name": []}) == []

    def test_ros2_object_path(self):
        msg = SimpleNamespace(
            name=["shoulder", "elbow"],
            position=[0.1, 0.2],
            velocity=[1.0, 2.0],
            effort=[],
        )
        rows = tx._rows_from_ros2("joint_state", "/joint_states", msg)
        assert [r["joint_name"] for r in rows] == ["shoulder", "elbow"]
        assert [r["position"] for r in rows] == [0.1, 0.2]
        assert [r["effort"] for r in rows] == [None, None]


# --- odometry ---------------------------------------------------------------

ODOM_PAYLOAD = {
    "pose": {
        "pose": {
            "position": {"x": 1.0, "y": 2.0, "z": 3.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.7071, "w": 0.7071},
        }
    },
    "twist": {
        "twist": {
            "linear": {"x": 0.5, "y": 0.0, "z": 0.0},
            "angular": {"x": 0.0, "y": 0.0, "z": 0.1},
        }
    },
}


class TestOdometry:
    def test_pose_and_twist_are_both_kept(self):
        """v1 keeps one (`extract_xyz` returns twist, or pose if twist is
        empty). Downstream consumers need both the pose and the twist."""
        rows = _json("nav_msgs/msg/Odometry", "/odom", ODOM_PAYLOAD)
        assert rows == [
            {
                "px": 1.0, "py": 2.0, "pz": 3.0,
                "qx": 0.0, "qy": 0.0, "qz": 0.7071, "qw": 0.7071,
                "vx": 0.5, "vy": 0.0, "vz": 0.0,
                "wx": 0.0, "wy": 0.0, "wz": 0.1,
            }
        ]

    def test_missing_sections_are_null(self):
        rows = _json("nav_msgs/msg/Odometry", "/odom", {})
        assert len(rows) == 1
        assert set(rows[0].values()) == {None}

    def test_ros2_object_path(self):
        msg = SimpleNamespace(
            pose=SimpleNamespace(
                pose=SimpleNamespace(
                    position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
                    orientation=SimpleNamespace(x=0.0, y=0.0, z=0.7071, w=0.7071),
                )
            ),
            twist=SimpleNamespace(
                twist=SimpleNamespace(
                    linear=SimpleNamespace(x=0.5, y=0.0, z=0.0),
                    angular=SimpleNamespace(x=0.0, y=0.0, z=0.1),
                )
            ),
        )
        assert tx._rows_from_ros2("odometry", "/odom", msg) == _json(
            "nav_msgs/msg/Odometry", "/odom", ODOM_PAYLOAD
        )


# --- twist ------------------------------------------------------------------

TWIST_PAYLOAD = {
    "linear": {"x": 0.4, "y": 0.0, "z": 0.0},
    "angular": {"x": 0.0, "y": 0.0, "z": -0.75},
}


class TestTwist:
    def test_all_six_components_are_kept(self):
        """The whole point of the family: v1 keeps `linear.{x,y,z}` and
        drops the angular half, so the COMMANDED yaw rate exists nowhere in
        the lake."""
        assert _json("geometry_msgs/msg/Twist", "/cmd_vel", TWIST_PAYLOAD) == [
            {
                "linear_x": 0.4, "linear_y": 0.0, "linear_z": 0.0,
                "angular_x": 0.0, "angular_y": 0.0, "angular_z": -0.75,
            }
        ]

    def test_a_missing_angular_section_is_null_not_zero(self):
        """NULL and 0.0 are different statements about a velocity command --
        "never recorded" versus "hold still" -- and downstream consumers
        must keep them apart."""
        rows = _json("geometry_msgs/msg/Twist", "/cmd_vel", {"linear": {"x": 1.0}})
        assert rows == [
            {
                "linear_x": 1.0, "linear_y": None, "linear_z": None,
                "angular_x": None, "angular_y": None, "angular_z": None,
            }
        ]

    def test_a_wrongly_typed_section_yields_nulls_rather_than_raising(self):
        rows = _json("geometry_msgs/msg/Twist", "/cmd_vel", {"linear": [1, 2, 3]})
        assert len(rows) == 1
        assert set(rows[0].values()) == {None}

    def test_non_finite_components_are_dropped(self):
        rows = extract_typed_rows(
            "geometry_msgs/msg/Twist",
            "/cmd_vel",
            "json",
            b'{"linear": {"x": 1e999}, "angular": {"z": 0.5}}',
        )
        assert rows[0]["linear_x"] is None
        assert rows[0]["angular_z"] == 0.5

    def test_ros2_object_path(self):
        msg = SimpleNamespace(
            linear=SimpleNamespace(x=0.4, y=0.0, z=0.0),
            angular=SimpleNamespace(x=0.0, y=0.0, z=-0.75),
        )
        assert tx._rows_from_ros2("twist", "/cmd_vel", msg) == _json(
            "geometry_msgs/msg/Twist", "/cmd_vel", TWIST_PAYLOAD
        )

    def test_one_row_per_message(self):
        """A Twist carries one command, unlike JointState/TFMessage which
        carry N records -- so the family is one row per message and the v1
        row count for the topic is unchanged."""
        assert len(_json("geometry_msgs/msg/Twist", "/cmd_vel", TWIST_PAYLOAD)) == 1


# --- tf ---------------------------------------------------------------------

TF_PAYLOAD = {
    "transforms": [
        {
            "header": {"frame_id": "odom"},
            "child_frame_id": "base_link",
            "transform": {
                "translation": {"x": 1.0, "y": 2.0, "z": 0.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            },
        },
        {
            "header": {"frame_id": "base_link"},
            "child_frame_id": "lidar",
            "transform": {
                "translation": {"x": 0.1, "y": 0.0, "z": 0.5},
                "rotation": {"x": 0.0, "y": 0.0, "z": 1.0, "w": 0.0},
            },
        },
    ]
}


class TestTF:
    def test_one_row_per_transform(self):
        rows = _json("tf2_msgs/msg/TFMessage", "/tf", TF_PAYLOAD)
        assert [(r["parent_frame"], r["child_frame"]) for r in rows] == [
            ("odom", "base_link"),
            ("base_link", "lidar"),
        ]
        assert rows[1]["tx"] == 0.1
        assert rows[1]["tz"] == 0.5
        assert rows[1]["qz"] == 1.0

    def test_is_static_comes_from_the_raw_topic(self):
        """`/tf` and `/tf_static` are the same type and normalize to the
        same `tf` bucket, so the calibration-vs-motion distinction only
        survives if it is read off the raw topic name."""
        assert _json("tf2_msgs/msg/TFMessage", "/tf", TF_PAYLOAD)[0]["is_static"] is False
        assert (
            _json("tf2_msgs/msg/TFMessage", "/tf_static", TF_PAYLOAD)[0]["is_static"]
            is True
        )
        assert (
            _json("tf2_msgs/msg/TFMessage", "/robot1/tf_static", TF_PAYLOAD)[0]["is_static"]
            is True
        )

    def test_is_static_topic_rule(self):
        assert is_static_tf_topic("/tf_static") is True
        assert is_static_tf_topic("tf_static/") is True
        assert is_static_tf_topic("/TF_STATIC") is True
        assert is_static_tf_topic("/tf") is False
        assert is_static_tf_topic("/tf_static_extra") is False
        assert is_static_tf_topic(None) is False

    def test_empty_transform_list_yields_no_rows(self):
        assert _json("tf2_msgs/msg/TFMessage", "/tf", {"transforms": []}) == []

    def test_ros2_object_path(self):
        msg = SimpleNamespace(
            transforms=[
                SimpleNamespace(
                    header=SimpleNamespace(frame_id="odom"),
                    child_frame_id="base_link",
                    transform=SimpleNamespace(
                        translation=SimpleNamespace(x=1.0, y=2.0, z=0.0),
                        rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
                    ),
                )
            ]
        )
        rows = tx._rows_from_ros2("tf", "/tf_static", msg)
        assert len(rows) == 1
        assert rows[0]["parent_frame"] == "odom"
        assert rows[0]["child_frame"] == "base_link"
        assert rows[0]["is_static"] is True


# --- camera_info ------------------------------------------------------------

class TestCameraInfo:
    K = [600.0, 0.0, 320.0, 0.0, 610.0, 240.0, 0.0, 0.0, 1.0]

    def test_intrinsics_come_out_of_the_k_matrix(self):
        rows = _json(
            "sensor_msgs/msg/CameraInfo",
            "/camera/camera_info",
            {
                "width": 640,
                "height": 480,
                "k": self.K,
                "distortion_model": "plumb_bob",
            },
        )
        assert rows == [
            {
                "width": 640, "height": 480,
                "fx": 600.0, "fy": 610.0, "cx": 320.0, "cy": 240.0,
                "distortion_model": "plumb_bob",
            }
        ]

    def test_ros1_style_uppercase_k_is_accepted(self):
        """rclpy names the field `k`; ROS 1 and several JSON encoders keep
        `K`. Both are the same 3x3 matrix."""
        rows = _json(
            "sensor_msgs/msg/CameraInfo",
            "/camera/camera_info",
            {"width": 8, "height": 8, "K": self.K},
        )
        assert (rows[0]["fx"], rows[0]["cy"]) == (600.0, 240.0)

    def test_uncalibrated_camera_is_null_intrinsics_not_zero(self):
        """An all-zero `k` is ROS's "not calibrated" marker. Recording
        0.0 focal lengths would look like a calibrated camera with absurd
        intrinsics; the row still carries the frame size."""
        rows = _json(
            "sensor_msgs/msg/CameraInfo",
            "/camera/camera_info",
            {"width": 640, "height": 480, "k": []},
        )
        assert rows[0]["width"] == 640
        assert (rows[0]["fx"], rows[0]["fy"], rows[0]["cx"], rows[0]["cy"]) == (
            None, None, None, None,
        )
        assert rows[0]["distortion_model"] is None

    def test_ros2_object_path(self):
        msg = SimpleNamespace(
            width=1280, height=720, k=self.K, distortion_model="rational_polynomial",
        )
        rows = tx._rows_from_ros2("camera_info", "/camera/camera_info", msg)
        assert rows[0]["width"] == 1280
        assert rows[0]["fx"] == 600.0
        assert rows[0]["distortion_model"] == "rational_polynomial"


# --- pointcloud_stats -------------------------------------------------------

POINT_STEP = 16  # x,y,z float32 + 4 bytes padding, the usual XYZI/XYZ layout
XYZ_FIELDS = [
    {"name": "x", "offset": 0, "datatype": 7},
    {"name": "y", "offset": 4, "datatype": 7},
    {"name": "z", "offset": 8, "datatype": 7},
]


def _cloud_bytes(points) -> bytes:
    return b"".join(struct.pack("<fff", *p) + b"\x00" * 4 for p in points)


def _cloud_payload(points, **overrides) -> dict:
    payload = {
        "fields": XYZ_FIELDS,
        "point_step": POINT_STEP,
        "width": len(points),
        "height": 1,
        "is_bigendian": False,
        "data": base64.b64encode(_cloud_bytes(points)).decode(),
    }
    payload.update(overrides)
    return payload


class TestPointCloudStats:
    def test_bounds_over_a_small_cloud(self):
        points = [(1.0, -2.0, 0.5), (3.0, 4.0, -1.5), (2.0, 0.0, 7.0)]
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", _cloud_payload(points))
        assert rows == [
            {
                "n_points": 3, "sample_n": 3,
                "x_min": 1.0, "x_max": 3.0,
                "y_min": -2.0, "y_max": 4.0,
                "z_min": -1.5, "z_max": 7.0,
            }
        ]

    def test_data_as_a_list_of_ints_is_accepted(self):
        """The other shape JSON encoders use for `uint8[]`."""
        points = [(1.0, 1.0, 1.0), (-1.0, -1.0, -1.0)]
        payload = _cloud_payload(points, data=list(_cloud_bytes(points)))
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", payload)
        assert rows[0]["sample_n"] == 2
        assert (rows[0]["x_min"], rows[0]["x_max"]) == (-1.0, 1.0)

    def test_sampling_is_bounded_and_strided(self):
        """A lidar sweep must not cost one Python unpack per point -- but
        the sample still has to span the WHOLE sweep rather than truncating
        its tail. The sentinel coordinates sit at both endpoints."""
        n = 5000
        points = [(float(i), float(-i), 0.0) for i in range(n)]
        points[0] = (-999.0, 0.0, 0.0)
        points[-1] = (9999.0, 0.0, 555.0)
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", _cloud_payload(points))
        row = rows[0]
        assert row["n_points"] == n
        assert 0 < row["sample_n"] <= tx.MAX_POINTCLOUD_SAMPLE
        assert row["x_min"] == -999.0
        assert row["x_max"] == 9999.0
        assert row["z_max"] == 555.0

    def test_sampling_positions_are_uniform_and_include_both_ends(self):
        indexes = tx._evenly_spaced_indices(5000, 1024)
        assert len(indexes) == 1024
        assert indexes[0] == 0
        assert indexes[-1] == 4999
        gaps = [right - left for left, right in zip(indexes, indexes[1:])]
        assert max(gaps) - min(gaps) <= 1

    def test_organized_cloud_honours_row_padding(self):
        """Padding bytes are not points, and the second row starts at
        ``row_step`` rather than ``width * point_step``."""
        first_row = _cloud_bytes([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        second_row = _cloud_bytes([(-1.0, -2.0, -3.0), (8.0, 9.0, 10.0)])
        # If decoded as a point, these bytes produce extreme finite floats and
        # make the failure unmistakable rather than merely shifting a row.
        padding = struct.pack("<fff", 1234.0, 2345.0, 3456.0) + b"PAD!"
        assert len(padding) == POINT_STEP
        data = first_row + padding + second_row + padding
        payload = _cloud_payload(
            [],
            width=2,
            height=2,
            row_step=3 * POINT_STEP,
            data=base64.b64encode(data).decode(),
        )

        row = _json("sensor_msgs/msg/PointCloud2", "/points", payload)[0]

        assert row["n_points"] == 4
        assert row["sample_n"] == 4
        assert (row["x_min"], row["x_max"]) == (-1.0, 8.0)
        assert (row["z_min"], row["z_max"]) == (-3.0, 10.0)

    def test_invalid_short_row_step_records_count_without_guessing(self):
        payload = _cloud_payload(
            [(1.0, 2.0, 3.0)] * 4,
            width=2,
            height=2,
            row_step=POINT_STEP,
        )
        row = _json("sensor_msgs/msg/PointCloud2", "/points", payload)[0]
        assert row["n_points"] == 4
        assert row["sample_n"] == 0
        assert row["x_min"] is None

    def test_explicit_zero_row_step_is_invalid_not_tightly_packed(self):
        payload = _cloud_payload(
            [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)],
            width=2,
            height=1,
            row_step=0,
        )
        row = _json("sensor_msgs/msg/PointCloud2", "/points", payload)[0]
        assert row["n_points"] == 2
        assert row["sample_n"] == 0

    def test_field_cannot_read_across_point_boundary(self):
        payload = _cloud_payload(
            [(1.0, 2.0, 3.0)],
            fields=[
                {"name": "x", "offset": 0, "datatype": 7},
                {"name": "y", "offset": 4, "datatype": 7},
                {"name": "z", "offset": POINT_STEP - 2, "datatype": 7},
            ],
        )
        row = _json("sensor_msgs/msg/PointCloud2", "/points", payload)[0]
        assert row["n_points"] == 1
        assert row["sample_n"] == 0

    def test_nan_points_are_skipped_not_propagated(self):
        """Lidar drivers write NaN for "no return". One of them in a
        min/max would turn every downstream aggregate into NaN."""
        points = [(float("nan"), float("nan"), float("nan")), (2.0, 2.0, 2.0)]
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", _cloud_payload(points))
        assert rows[0]["n_points"] == 2
        assert rows[0]["sample_n"] == 1
        assert rows[0]["x_min"] == 2.0
        assert all(
            v is None or math.isfinite(v)
            for k, v in rows[0].items()
        )

    def test_cloud_without_xyz_fields_records_the_count_only(self):
        """An intensity-only or unreadable layout still gets a row: the
        point count is real information, the bounds are honestly NULL."""
        payload = _cloud_payload([(1.0, 2.0, 3.0)], fields=[
            {"name": "intensity", "offset": 0, "datatype": 7},
        ])
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", payload)
        assert rows[0]["n_points"] == 1
        assert rows[0]["sample_n"] == 0
        assert rows[0]["x_min"] is None and rows[0]["z_max"] is None

    def test_truncated_payload_does_not_rewrite_the_declared_count(self):
        """`n_points` is what the message claims; `sample_n` is what could
        actually be read. Keeping them separate is what makes a truncated
        cloud visible instead of quietly consistent."""
        points = [(1.0, 1.0, 1.0), (2.0, 2.0, 2.0), (3.0, 3.0, 3.0)]
        truncated = _cloud_bytes(points)[: POINT_STEP]  # only the first point
        payload = _cloud_payload(points, data=base64.b64encode(truncated).decode())
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", payload)
        assert rows[0]["n_points"] == 3
        assert rows[0]["sample_n"] == 1

    def test_float64_and_bigendian_layouts(self):
        """The datatype enum and `is_bigendian` are read from the message,
        not assumed: a float64 cloud parsed as float32 would produce
        garbage bounds rather than an error."""
        points = [(1.5, 2.5, 3.5), (-1.5, -2.5, -3.5)]
        data = b"".join(struct.pack(">ddd", *p) for p in points)
        payload = {
            "fields": [
                {"name": "x", "offset": 0, "datatype": 8},
                {"name": "y", "offset": 8, "datatype": 8},
                {"name": "z", "offset": 16, "datatype": 8},
            ],
            "point_step": 24,
            "width": 2,
            "height": 1,
            "is_bigendian": True,
            "data": base64.b64encode(data).decode(),
        }
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", payload)
        assert (rows[0]["x_min"], rows[0]["x_max"]) == (-1.5, 1.5)
        assert (rows[0]["z_min"], rows[0]["z_max"]) == (-3.5, 3.5)

    def test_organized_cloud_counts_width_times_height(self):
        points = [(float(i), 0.0, 0.0) for i in range(6)]
        payload = _cloud_payload(points, width=3, height=2)
        rows = _json("sensor_msgs/msg/PointCloud2", "/points", payload)
        assert rows[0]["n_points"] == 6
        assert rows[0]["sample_n"] == 6

    def test_ros2_object_path(self):
        points = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]
        msg = SimpleNamespace(
            fields=[
                SimpleNamespace(name="x", offset=0, datatype=7),
                SimpleNamespace(name="y", offset=4, datatype=7),
                SimpleNamespace(name="z", offset=8, datatype=7),
            ],
            point_step=POINT_STEP,
            width=2,
            height=1,
            is_bigendian=False,
            data=bytearray(_cloud_bytes(points)),
        )
        rows = tx._rows_from_ros2("pointcloud_stats", "/points", msg)
        assert rows[0]["n_points"] == 2
        assert (rows[0]["x_min"], rows[0]["x_max"]) == (1.0, 4.0)


# --- schema-bearing CDR round-trips (no ROS runtime) ------------------------

class TestCdrRoundTrip:
    """Rosbags CDR decoded against each MCAP's embedded ros2msg schema."""

    def _cdr(self, msg, schema: str, topic: str):
        schema_record = Schema(
            id=1,
            name=schema,
            encoding="ros2msg",
            data=sim_messages.message_definition(schema).encode("utf-8"),
        )

        return extract_typed_rows(
            schema,
            topic,
            "cdr",
            sim_messages.serialize(msg, schema),
            cdr_decoder=McapRos2Decoder(),
            schema=schema_record,
        )

    def test_joint_state(self):
        msg = sim_messages.make_joint_state(
            0, "base_link", [1.5, -2.5], [0.25, -0.25]
        )
        rows = self._cdr(msg, "sensor_msgs/msg/JointState", "/joint_states")
        # The simulation fixture names the two joints explicitly; this pins
        # the exact strings that travelled through CDR, rather than silently
        # relying on a hand-built ROS message with different names.
        assert [r["joint_name"] for r in rows] == [
            "left_wheel_joint", "right_wheel_joint"
        ]
        assert [r["position"] for r in rows] == [1.5, -2.5]
        assert [r["effort"] for r in rows] == [None, None]

    def test_odometry(self):
        msg = sim_messages.make_odometry(
            0, "odom", "base_link", 1.0, 0.0, 0.0, 0.5, 0.1
        )
        rows = self._cdr(msg, "nav_msgs/msg/Odometry", "/odom")
        assert rows[0]["px"] == 1.0
        assert rows[0]["qw"] == 1.0
        assert rows[0]["vx"] == 0.5
        assert rows[0]["wz"] == 0.1

    def test_twist(self):
        msg = sim_messages.make_twist(0.4, -0.75)
        rows = self._cdr(msg, "geometry_msgs/msg/Twist", "/cmd_vel")
        assert rows == [
            {
                "linear_x": 0.4, "linear_y": 0.0, "linear_z": 0.0,
                "angular_x": 0.0, "angular_y": 0.0, "angular_z": -0.75,
            }
        ]

    def test_tf(self):
        msg = sim_messages.make_tf(0, "odom", "base_link", 2.0, 0.0, 0.0)
        rows = self._cdr(msg, "tf2_msgs/msg/TFMessage", "/tf_static")
        assert rows == [
            {
                "parent_frame": "odom", "child_frame": "base_link",
                "tx": 2.0, "ty": 0.0, "tz": 0.0,
                "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0,
                "is_static": True,
            }
        ]

    def test_camera_info(self):
        msg = sim_messages.make_camera_info(
            0,
            "camera",
            640,
            480,
            k=[600.0, 0.0, 320.0, 0.0, 610.0, 240.0, 0.0, 0.0, 1.0],
        )
        rows = self._cdr(msg, "sensor_msgs/msg/CameraInfo", "/camera/camera_info")
        assert rows[0]["width"] == 640
        assert rows[0]["fx"] == 600.0
        assert rows[0]["cy"] == 240.0
        assert rows[0]["distortion_model"] == "plumb_bob"

    def test_pointcloud2(self):
        points = [(1.0, -2.0, 0.5), (3.0, 4.0, -1.5)]
        msg = PointCloud2(
            header=Header(stamp=Time(sec=0, nanosec=0), frame_id="base_link"),
            height=1,
            width=len(points),
            fields=[
                PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
                PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
                PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
            ],
            is_bigendian=False,
            point_step=POINT_STEP,
            row_step=POINT_STEP * len(points),
            data=np.frombuffer(_cloud_bytes(points), dtype=np.uint8),
            is_dense=True,
        )
        rows = self._cdr(msg, "sensor_msgs/msg/PointCloud2", "/points")
        assert rows[0]["n_points"] == 2
        assert rows[0]["sample_n"] == 2
        assert (rows[0]["x_min"], rows[0]["x_max"]) == (1.0, 3.0)
        assert (rows[0]["z_min"], rows[0]["z_max"]) == (-1.5, 0.5)

    def test_undecodable_cdr_bytes_yield_no_rows(self):
        """A corrupt payload must produce nothing, not a row of nulls that
        looks like a real measurement."""
        schema = Schema(
            id=1,
            name="nav_msgs/msg/Odometry",
            encoding="ros2msg",
            data=sim_messages.message_definition("nav_msgs/msg/Odometry").encode("utf-8"),
        )
        assert extract_typed_rows(
            "nav_msgs/msg/Odometry",
            "/odom",
            "cdr",
            b"\x00\x01\x02\x03",
            cdr_decoder=McapRos2Decoder(),
            schema=schema,
        ) == []
