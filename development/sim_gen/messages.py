"""ROS 2 message construction for the simulated AMR, via `rosbags` typestores.

Why rosbags (Ternaris, Apache-2.0) and not a ROS install: the dev image is a
plain `python:3.12-slim` and must stay that way. rosbags ships the ROS 2 Humble
type definitions as pure-Python dataclasses plus a CDR (de)serializer, so we
can emit real, byte-compatible `cdr` payloads with no ROS on the box -- and the
acceptance test then decodes them against the embedded `ros2msg` definition via
the processor's pure-Python `mcap-ros2-support` path (see `verify_cdr.py`).

Every builder below takes an absolute timestamp in nanoseconds so the message
header stamp always matches the MCAP log_time.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

import numpy as np
from rosbags.typesys import Stores, get_typestore
from rosbags.typesys.stores.ros2_humble import (
    builtin_interfaces__msg__Time as Time,
    geometry_msgs__msg__Point as Point,
    geometry_msgs__msg__Pose as Pose,
    geometry_msgs__msg__PoseWithCovariance as PoseWithCovariance,
    geometry_msgs__msg__Quaternion as Quaternion,
    geometry_msgs__msg__Transform as Transform,
    geometry_msgs__msg__TransformStamped as TransformStamped,
    geometry_msgs__msg__Twist as Twist,
    geometry_msgs__msg__TwistWithCovariance as TwistWithCovariance,
    geometry_msgs__msg__Vector3 as Vector3,
    nav_msgs__msg__Odometry as Odometry,
    sensor_msgs__msg__CameraInfo as CameraInfo,
    sensor_msgs__msg__CompressedImage as CompressedImage,
    sensor_msgs__msg__Imu as Imu,
    sensor_msgs__msg__JointState as JointState,
    sensor_msgs__msg__LaserScan as LaserScan,
    sensor_msgs__msg__NavSatFix as NavSatFix,
    sensor_msgs__msg__NavSatStatus as NavSatStatus,
    sensor_msgs__msg__RegionOfInterest as RegionOfInterest,
    std_msgs__msg__Header as Header,
    std_msgs__msg__String as StringMsg,
    tf2_msgs__msg__TFMessage as TFMessage,
)

TYPESTORE = get_typestore(Stores.ROS2_HUMBLE)

#: Fixed GPS origin the simulated fleet drives around.
ORIGIN_LATITUDE = 37.774900
ORIGIN_LONGITUDE = -122.419400
ORIGIN_ALTITUDE_M = 12.0
_METRES_PER_DEG_LAT = 111320.0

WHEEL_JOINT_NAMES = ["left_wheel_joint", "right_wheel_joint"]


def serialize(msg: object, msgtype: str) -> bytes:
    """CDR-encode a message (little-endian, with encapsulation header)."""
    return bytes(TYPESTORE.serialize_cdr(msg, msgtype))


def message_definition(msgtype: str) -> str:
    """Concatenated ROS 2 `.msg` definition text for an MCAP `ros2msg` schema."""
    msgdef, _md5 = TYPESTORE.generate_msgdef(msgtype, ros_version=2)
    return msgdef


def _stamp(time_ns: int) -> Time:
    return Time(sec=int(time_ns // 1_000_000_000), nanosec=int(time_ns % 1_000_000_000))


def _header(time_ns: int, frame_id: str) -> Header:
    return Header(stamp=_stamp(time_ns), frame_id=frame_id)


def _quat_from_yaw(yaw: float) -> Quaternion:
    return Quaternion(x=0.0, y=0.0, z=math.sin(yaw / 2.0), w=math.cos(yaw / 2.0))


def _quat_from_rpy(roll: float, pitch: float, yaw: float) -> Quaternion:
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return Quaternion(
        x=sr * cp * cy - cr * sp * sy,
        y=cr * sp * cy + sr * cp * sy,
        z=cr * cp * sy - sr * sp * cy,
        w=cr * cp * cy + sr * sp * sy,
    )


def _diag(values: Sequence[float], size: int) -> np.ndarray:
    side = int(math.isqrt(size))
    cov = np.zeros(size, dtype=np.float64)
    for i, v in enumerate(values[:side]):
        cov[i * side + i] = float(v)
    return cov


_POSE_COV = _diag([0.02, 0.02, 1e6, 1e6, 1e6, 0.05], 36)
_TWIST_COV = _diag([0.01, 1e6, 1e6, 1e6, 1e6, 0.02], 36)
_ORIENT_COV = _diag([0.005, 0.005, 0.01], 9)
_GYRO_COV = _diag([0.0004, 0.0004, 0.0004], 9)
_ACCEL_COV = _diag([0.004, 0.004, 0.004], 9)
_GPS_COV = _diag([2.5, 2.5, 5.0], 9)


def make_string(text: str) -> StringMsg:
    """std_msgs/String has no header, so this one carries no stamp -- the MCAP
    log_time is the only timestamp, which is exactly how a real task marker
    published by a task manager looks."""
    return StringMsg(data=text)


def make_compressed_image(time_ns: int, frame_id: str, jpeg: bytes) -> CompressedImage:
    return CompressedImage(
        header=_header(time_ns, frame_id),
        format="jpeg",
        data=np.frombuffer(jpeg, dtype=np.uint8),
    )


def make_camera_info(
    time_ns: int,
    frame_id: str,
    width: int,
    height: int,
    *,
    k: Sequence[float] = (),
    d: Sequence[float] = (),
    distortion_model: str = "plumb_bob",
) -> CameraInfo:
    # Plausible pinhole intrinsics for a 640x480 sensor, scaled if the caller
    # asked for a different frame size (tests use tiny frames for speed).
    if len(k) >= 9:
        fx, cx, fy, cy = float(k[0]), float(k[2]), float(k[4]), float(k[5])
    else:
        fx = 525.0 * (width / 640.0)
        fy = 525.0 * (height / 480.0)
        cx = (width - 1) / 2.0
        cy = (height - 1) / 2.0
    distortion = list(d) if d else [0.0223, -0.0541, 0.0007, -0.0002, 0.0]
    return CameraInfo(
        header=_header(time_ns, frame_id),
        height=int(height),
        width=int(width),
        distortion_model=distortion_model,
        d=np.array(distortion, dtype=np.float64),
        k=np.array([fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0], dtype=np.float64),
        r=np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], dtype=np.float64),
        p=np.array(
            [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0], dtype=np.float64
        ),
        binning_x=0,
        binning_y=0,
        roi=RegionOfInterest(
            x_offset=0, y_offset=0, height=0, width=0, do_rectify=False
        ),
    )


def make_twist(linear_x: float, angular_z: float) -> Twist:
    return Twist(
        linear=Vector3(x=float(linear_x), y=0.0, z=0.0),
        angular=Vector3(x=0.0, y=0.0, z=float(angular_z)),
    )


def make_odometry(
    time_ns: int,
    frame_id: str,
    child_frame_id: str,
    x: float,
    y: float,
    theta: float,
    v: float,
    w: float,
) -> Odometry:
    return Odometry(
        header=_header(time_ns, frame_id),
        child_frame_id=child_frame_id,
        pose=PoseWithCovariance(
            pose=Pose(
                position=Point(x=float(x), y=float(y), z=0.0),
                orientation=_quat_from_yaw(float(theta)),
            ),
            covariance=_POSE_COV.copy(),
        ),
        twist=TwistWithCovariance(
            twist=make_twist(v, w),
            covariance=_TWIST_COV.copy(),
        ),
    )


def make_joint_state(
    time_ns: int,
    frame_id: str,
    positions: Sequence[float],
    velocities: Sequence[float],
    *,
    names: Sequence[str] = WHEEL_JOINT_NAMES,
    efforts: Sequence[float] = (),
) -> JointState:
    return JointState(
        header=_header(time_ns, frame_id),
        name=list(names),
        position=np.array(positions, dtype=np.float64),
        velocity=np.array(velocities, dtype=np.float64),
        effort=np.array(efforts, dtype=np.float64),
    )


def make_laser_scan(
    time_ns: int,
    frame_id: str,
    *,
    angle_min: float,
    angle_max: float,
    angle_increment: float,
    time_increment: float,
    scan_time: float,
    range_min: float,
    range_max: float,
    ranges: Sequence[float],
    intensities: Sequence[float],
) -> LaserScan:
    """Build a ROS 2 LaserScan while preserving a ROS 1 source scan's values."""
    return LaserScan(
        header=_header(time_ns, frame_id),
        angle_min=float(angle_min),
        angle_max=float(angle_max),
        angle_increment=float(angle_increment),
        time_increment=float(time_increment),
        scan_time=float(scan_time),
        range_min=float(range_min),
        range_max=float(range_max),
        ranges=np.array(ranges, dtype=np.float32),
        intensities=np.array(intensities, dtype=np.float32),
    )


def make_tf(
    time_ns: int, parent: str, child: str, x: float, y: float, theta: float
) -> TFMessage:
    return TFMessage(
        transforms=[
            TransformStamped(
                header=_header(time_ns, parent),
                child_frame_id=child,
                transform=Transform(
                    translation=Vector3(x=float(x), y=float(y), z=0.0),
                    rotation=_quat_from_yaw(float(theta)),
                ),
            )
        ]
    )


def make_tf_static(time_ns: int, parent: str, child: str) -> TFMessage:
    """base_link -> camera_link: the extrinsic a calibration check looks for."""
    return TFMessage(
        transforms=[
            TransformStamped(
                header=_header(time_ns, parent),
                child_frame_id=child,
                transform=Transform(
                    translation=Vector3(x=0.20, y=0.0, z=0.45),
                    rotation=_quat_from_rpy(0.0, 0.15, 0.0),
                ),
            )
        ]
    )


def make_imu(
    time_ns: int,
    frame_id: str,
    theta: float,
    gyro: Tuple[float, float, float],
    accel: Tuple[float, float, float],
) -> Imu:
    return Imu(
        header=_header(time_ns, frame_id),
        orientation=_quat_from_yaw(float(theta)),
        orientation_covariance=_ORIENT_COV.copy(),
        angular_velocity=Vector3(x=float(gyro[0]), y=float(gyro[1]), z=float(gyro[2])),
        angular_velocity_covariance=_GYRO_COV.copy(),
        linear_acceleration=Vector3(
            x=float(accel[0]), y=float(accel[1]), z=float(accel[2])
        ),
        linear_acceleration_covariance=_ACCEL_COV.copy(),
    )


def make_navsatfix(
    time_ns: int, frame_id: str, x: float, y: float, drift: Tuple[float, float, float]
) -> NavSatFix:
    lat = ORIGIN_LATITUDE + y / _METRES_PER_DEG_LAT + drift[0]
    lon = ORIGIN_LONGITUDE + x / (
        _METRES_PER_DEG_LAT * math.cos(math.radians(ORIGIN_LATITUDE))
    ) + drift[1]
    return NavSatFix(
        header=_header(time_ns, frame_id),
        status=NavSatStatus(status=0, service=1),  # STATUS_FIX, SERVICE_GPS
        latitude=float(lat),
        longitude=float(lon),
        altitude=float(ORIGIN_ALTITUDE_M + drift[2]),
        position_covariance=_GPS_COV.copy(),
        position_covariance_type=2,  # COVARIANCE_TYPE_DIAGONAL_KNOWN
    )
