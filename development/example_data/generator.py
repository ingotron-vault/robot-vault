"""Build the four ship-with-alpha Robot Vault example recordings.

The MCAP writing and ROS 2 CDR message construction are intentionally reused
from :mod:`sim_gen`, the exact acceptance-smoke generator.  This module adds
source adapters around that proven path:

* TUM's ROS 1 Pioneer recording -> short, rebased ROS 2 CDR MCAP;
* LeRobot ALOHA Parquet -> full joint state/target rows plus an explicitly
  documented two-component action projection for the refinery's Twist family;
* a QA-imperfect TUM derivative whose faults are bytes in the recording, never
  fabricated database rows;
* a fully synthetic privacy fixture derived from the acceptance simulation,
  with a fictional generated face and a deterministic fictional plate.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pyarrow.parquet as pq
from mcap.reader import make_reader
from mcap.writer import CompressionType
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from rosbags.highlevel import AnyReader

from sim_gen import messages as M
from sim_gen.generator import (
    DEFAULT_START_TIME_NS,
    McapWriter,
    TopicSpec,
    generate as generate_sim,
)

EXAMPLE_DATA_VERSION = 1

TUM_SEQUENCE = "rgbd_dataset_freiburg2_pioneer_slam2"
TUM_FILENAME = f"{TUM_SEQUENCE}.bag"
TUM_URL = (
    "https://cvg.cit.tum.de/rgbd/dataset/freiburg2/"
    f"{TUM_FILENAME}"
)
TUM_DATASET_URL = "https://cvg.cit.tum.de/data/datasets/rgbd-dataset"
TUM_DOWNLOAD_URL = "https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download"
TUM_LICENSE = "Creative Commons Attribution 4.0 International (CC BY 4.0)"
TUM_ATTRIBUTION = (
    "Technical University of Munich, Computer Vision Group, TUM RGB-D "
    "Benchmark, sequence fr2/pioneer_slam2, CC BY 4.0."
)

ALOHA_REVISION = "b144896feb1f37398a862927b22cd3abdf005a6b"
ALOHA_FILENAME = "aloha_static_coffee-file-000.parquet"
ALOHA_URL = (
    "https://huggingface.co/datasets/lerobot/aloha_static_coffee/resolve/"
    f"{ALOHA_REVISION}/data/chunk-000/file-000.parquet"
)
ALOHA_CARD_URL = "https://huggingface.co/datasets/lerobot/aloha_static_coffee"
ALOHA_EXPECTED_SHA256 = "ed62be90cd794f6672b26485be2b01af47409ba4cd31042fe261ddc79090bcc1"
ALOHA_LICENSE = "MIT"
ALOHA_ATTRIBUTION = (
    "LeRobot aloha_static_coffee dataset (ALOHA; Tony Zhao, Vikash Kumar, "
    "Sergey Levine, and Chelsea Finn), MIT License."
)

OUTPUT_WIDTH = 480
OUTPUT_HEIGHT = 360
JPEG_QUALITY = 72
TURN_THRESHOLD_RAD_S = 0.05
ALOHA_JOINT_NAMES: Tuple[str, ...] = (
    "left_waist",
    "left_shoulder",
    "left_elbow",
    "left_forearm_roll",
    "left_wrist_angle",
    "left_wrist_rotate",
    "left_gripper",
    "right_waist",
    "right_shoulder",
    "right_elbow",
    "right_forearm_roll",
    "right_wrist_angle",
    "right_wrist_rotate",
    "right_gripper",
)

TOPIC_TASK = "/session/task"
TOPIC_TF_STATIC = "/tf_static"
TOPIC_TF = "/tf"
TOPIC_IMAGE = "/camera/rgb/image_compressed"
TOPIC_CAMERA_INFO = "/camera/rgb/camera_info"
TOPIC_MEASURED_TWIST = "/measured_twist"
TOPIC_ODOM = "/odom"
TOPIC_SCAN = "/scan"


@dataclass(frozen=True)
class TimedImage:
    offset_ns: int
    jpeg: bytes


@dataclass(frozen=True)
class TimedTwist:
    offset_ns: int
    linear_x: float
    linear_y: float
    linear_z: float
    angular_x: float
    angular_y: float
    angular_z: float


@dataclass(frozen=True)
class TimedOdom:
    offset_ns: int
    x: float
    y: float
    yaw: float
    linear_x: float
    angular_z: float


@dataclass(frozen=True)
class TimedScan:
    offset_ns: int
    angle_min: float
    angle_max: float
    angle_increment: float
    time_increment: float
    scan_time: float
    range_min: float
    range_max: float
    ranges: Tuple[float, ...]
    intensities: Tuple[float, ...]


@dataclass(frozen=True)
class CameraCalibration:
    width: int
    height: int
    k: Tuple[float, ...]
    d: Tuple[float, ...]
    distortion_model: str


@dataclass(frozen=True)
class TumClip:
    source_start_ns: int
    duration_ns: int
    images: Tuple[TimedImage, ...]
    twists: Tuple[TimedTwist, ...]
    odometry: Tuple[TimedOdom, ...]
    scans: Tuple[TimedScan, ...]
    calibration: CameraCalibration
    source_topics: Mapping[str, str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, *, expected_sha256: Optional[str] = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    print(f"downloading {url} -> {destination}")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(_HashingReader(response, digest), out, length=8 * 1024 * 1024)
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256:
            raise RuntimeError(
                f"download digest mismatch for {destination.name}: "
                f"expected {expected_sha256}, got {actual}"
            )
        os.replace(partial, destination)
    finally:
        if partial.exists():
            partial.unlink()


class _HashingReader:
    def __init__(self, source, digest) -> None:
        self.source = source
        self.digest = digest

    def read(self, size=-1):
        data = self.source.read(size)
        self.digest.update(data)
        return data


def _ensure_source(
    path: Path,
    *,
    url: str,
    download: bool,
    expected_sha256: Optional[str] = None,
) -> str:
    if not path.exists():
        if not download:
            raise FileNotFoundError(
                f"source missing: {path}; supply it explicitly or omit --no-download"
            )
        _download(url, path, expected_sha256=expected_sha256)
    digest = _sha256(path)
    if expected_sha256 and digest != expected_sha256:
        raise RuntimeError(
            f"source digest mismatch for {path}: expected {expected_sha256}, got {digest}"
        )
    return digest


def _connection(reader: AnyReader, topics: Sequence[str], msgtypes: Sequence[str]):
    by_topic = {connection.topic: connection for connection in reader.connections}
    for topic in topics:
        if topic in by_topic and by_topic[topic].msgtype in msgtypes:
            return by_topic[topic]
    lowered = [(connection.topic.lower(), connection) for connection in reader.connections]
    for topic in topics:
        suffix = topic.lower().strip("/")
        for actual, connection in lowered:
            if actual.strip("/").endswith(suffix) and connection.msgtype in msgtypes:
                return connection
    available = sorted(
        f"{connection.topic}:{connection.msgtype}" for connection in reader.connections
    )
    raise RuntimeError(
        f"TUM source lacks required topic among {list(topics)}; available: {available}"
    )


def _vector(obj) -> Tuple[float, float, float]:
    return (float(obj.x), float(obj.y), float(obj.z))


def _twist_values(msg) -> Tuple[float, float, float, float, float, float]:
    value = getattr(msg, "twist", msg)
    if hasattr(value, "twist"):
        value = value.twist
    linear = _vector(value.linear)
    angular = _vector(value.angular)
    return (*linear, *angular)


def _yaw(quaternion) -> float:
    x, y, z, w = (
        float(quaternion.x),
        float(quaternion.y),
        float(quaternion.z),
        float(quaternion.w),
    )
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _frame_id(msg, fallback: str) -> str:
    value = getattr(getattr(msg, "header", None), "frame_id", "")
    return str(value).strip().lstrip("/") or fallback


def _image_to_jpeg(msg) -> bytes:
    if hasattr(msg, "format") and not hasattr(msg, "encoding"):
        image = Image.open(io.BytesIO(bytes(msg.data))).convert("RGB")
    else:
        width, height = int(msg.width), int(msg.height)
        encoding = str(msg.encoding).lower()
        channels = 1 if encoding in ("mono8", "8uc1") else 4 if "rgba" in encoding or "bgra" in encoding else 3
        raw = np.frombuffer(bytes(msg.data), dtype=np.uint8)
        step = int(getattr(msg, "step", width * channels))
        rows = raw[: step * height].reshape(height, step)
        pixels = rows[:, : width * channels].reshape(height, width, channels)
        if channels == 1:
            image = Image.fromarray(pixels[:, :, 0], mode="L").convert("RGB")
        elif encoding.startswith("bgr"):
            image = Image.fromarray(pixels[:, :, :3][:, :, ::-1], mode="RGB")
        else:
            image = Image.fromarray(pixels[:, :, :3], mode="RGB")
    image = image.resize((OUTPUT_WIDTH, OUTPUT_HEIGHT), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _angle_delta(after: float, before: float) -> float:
    return math.atan2(math.sin(after - before), math.cos(after - before))


def _find_turn_window(reader: AnyReader, twist_connection, odom_connection, duration_ns: int) -> int:
    earliest = reader.start_time
    latest_start = reader.end_time - duration_ns
    if twist_connection is not None:
        for connection, timestamp, raw in reader.messages(connections=[twist_connection]):
            if timestamp < earliest + 1_000_000_000 or timestamp > latest_start:
                continue
            values = _twist_values(reader.deserialize(raw, connection.msgtype))
            if abs(values[5]) >= TURN_THRESHOLD_RAD_S:
                return max(earliest, timestamp - 1_000_000_000)
    else:
        previous = None
        for connection, timestamp, raw in reader.messages(connections=[odom_connection]):
            msg = reader.deserialize(raw, connection.msgtype)
            yaw = _yaw(msg.pose.pose.orientation)
            if previous is not None:
                previous_timestamp, previous_yaw = previous
                dt = (timestamp - previous_timestamp) / 1_000_000_000
                angular_z = _angle_delta(yaw, previous_yaw) / dt if dt > 0 else 0.0
                if (
                    timestamp >= earliest + 1_000_000_000
                    and timestamp <= latest_start
                    and abs(angular_z) >= TURN_THRESHOLD_RAD_S
                ):
                    return max(earliest, timestamp - 1_000_000_000)
            previous = (timestamp, yaw)
    raise RuntimeError(
        "TUM source has no complete excerpt containing real non-zero angular motion"
    )


def _sample_allowed(last_ns: Optional[int], timestamp: int, period_ns: int) -> bool:
    return last_ns is None or timestamp - last_ns >= period_ns


def read_tum_clip(
    path: Path,
    *,
    duration_seconds: float,
    start_seconds: Optional[float],
) -> TumClip:
    duration_ns = int(round(duration_seconds * 1_000_000_000))
    with AnyReader([path]) as reader:
        image_connection = _connection(
            reader,
            (
                "/camera/rgb/image_color",
                "/camera/rgb/image_raw",
                "/camera/rgb/image_rect_color",
                "/camera/rgb/image_color/compressed",
            ),
            ("sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage"),
        )
        info_connection = _connection(
            reader,
            ("/camera/rgb/camera_info",),
            ("sensor_msgs/msg/CameraInfo",),
        )
        odom_connection = _connection(
            reader,
            ("/odom", "/pose", "/base_pose_ground_truth"),
            ("nav_msgs/msg/Odometry",),
        )
        try:
            twist_connection = _connection(
                reader,
                ("/base_controller/command", "/cmd_vel", "/mobile_base/commands/velocity"),
                ("geometry_msgs/msg/Twist", "geometry_msgs/msg/TwistStamped"),
            )
        except RuntimeError:
            # The canonical TUM Pioneer bags publish mocap-backed `/pose`
            # Odometry but no command topic. In that case we derive measured
            # linear/angular velocity from the real pose trajectory below;
            # we never relabel it as a command.
            twist_connection = None
        scan_connection = _connection(
            reader,
            ("/scan", "/base_scan"),
            ("sensor_msgs/msg/LaserScan",),
        )
        if start_seconds is None:
            start_ns = _find_turn_window(
                reader, twist_connection, odom_connection, duration_ns
            )
        else:
            start_ns = reader.start_time + int(round(start_seconds * 1_000_000_000))
        stop_ns = start_ns + duration_ns
        if start_ns < reader.start_time or stop_ns > reader.end_time:
            raise RuntimeError(
                f"TUM excerpt [{start_ns}, {stop_ns}) is outside source "
                f"[{reader.start_time}, {reader.end_time})"
            )

        images: List[TimedImage] = []
        twists: List[TimedTwist] = []
        odometry: List[TimedOdom] = []
        scans: List[TimedScan] = []
        calibration: Optional[CameraCalibration] = None
        last: Dict[str, int] = {}
        wanted = [
            image_connection,
            info_connection,
            odom_connection,
            scan_connection,
        ]
        if twist_connection is not None:
            wanted.append(twist_connection)
        for connection, timestamp, raw in reader.messages(
            connections=wanted, start=start_ns, stop=stop_ns
        ):
            msg = reader.deserialize(raw, connection.msgtype)
            offset_ns = timestamp - start_ns
            if connection.id == image_connection.id:
                if not _sample_allowed(last.get("image"), timestamp, 200_000_000):
                    continue
                last["image"] = timestamp
                images.append(TimedImage(offset_ns, _image_to_jpeg(msg)))
            elif connection.id == info_connection.id and calibration is None:
                source_width, source_height = int(msg.width), int(msg.height)
                scale_x = OUTPUT_WIDTH / max(1, source_width)
                scale_y = OUTPUT_HEIGHT / max(1, source_height)
                # ROS 1 CameraInfo spells the matrix fields `K`/`D`; ROS 2
                # uses lowercase attributes in the generated rosbags types.
                # The source adapter accepts either without changing values.
                source_k_values = getattr(msg, "k", None)
                if source_k_values is None:
                    source_k_values = getattr(msg, "K")
                source_d_values = getattr(msg, "d", None)
                if source_d_values is None:
                    source_d_values = getattr(msg, "D")
                source_k = [float(value) for value in source_k_values]
                scaled_k = (
                    source_k[0] * scale_x,
                    source_k[1],
                    source_k[2] * scale_x,
                    source_k[3],
                    source_k[4] * scale_y,
                    source_k[5] * scale_y,
                    source_k[6],
                    source_k[7],
                    source_k[8],
                )
                calibration = CameraCalibration(
                    OUTPUT_WIDTH,
                    OUTPUT_HEIGHT,
                    tuple(scaled_k),
                    tuple(float(value) for value in source_d_values),
                    str(msg.distortion_model),
                )
            elif twist_connection is not None and connection.id == twist_connection.id:
                if not _sample_allowed(last.get("twist"), timestamp, 100_000_000):
                    continue
                last["twist"] = timestamp
                values = _twist_values(msg)
                twists.append(TimedTwist(offset_ns, *values))
            elif connection.id == odom_connection.id:
                if not _sample_allowed(last.get("odom"), timestamp, 50_000_000):
                    continue
                last["odom"] = timestamp
                pose = msg.pose.pose
                twist = msg.twist.twist
                odometry.append(
                    TimedOdom(
                        offset_ns,
                        float(pose.position.x),
                        float(pose.position.y),
                        _yaw(pose.orientation),
                        float(twist.linear.x),
                        float(twist.angular.z),
                    )
                )
            elif connection.id == scan_connection.id:
                if not _sample_allowed(last.get("scan"), timestamp, 200_000_000):
                    continue
                last["scan"] = timestamp
                scans.append(
                    TimedScan(
                        offset_ns,
                        float(msg.angle_min),
                        float(msg.angle_max),
                        float(msg.angle_increment),
                        float(msg.time_increment),
                        float(msg.scan_time),
                        float(msg.range_min),
                        float(msg.range_max),
                        tuple(float(value) for value in msg.ranges),
                        tuple(float(value) for value in msg.intensities),
                    )
                )

        if twist_connection is None:
            odometry, twists = _derive_pose_twist(odometry)
        if calibration is None:
            raise RuntimeError("selected TUM excerpt carries no CameraInfo")
        counts = {
            "camera": len(images),
            "twist": len(twists),
            "odometry": len(odometry),
            "laser": len(scans),
        }
        if any(value < 2 for value in counts.values()):
            raise RuntimeError(f"selected TUM excerpt is incomplete: {counts}")
        if not any(abs(value.angular_z) >= TURN_THRESHOLD_RAD_S for value in twists):
            raise RuntimeError("selected TUM excerpt did not preserve real angular_z")
        return TumClip(
            source_start_ns=start_ns,
            duration_ns=duration_ns,
            images=tuple(images),
            twists=tuple(twists),
            odometry=tuple(odometry),
            scans=tuple(scans),
            calibration=calibration,
            source_topics={
                "camera": image_connection.topic,
                "camera_info": info_connection.topic,
                "twist": (
                    twist_connection.topic
                    if twist_connection is not None
                    else f"derived measured velocity from {odom_connection.topic} pose trajectory"
                ),
                "odometry": odom_connection.topic,
                "laser": scan_connection.topic,
            },
        )


def _derive_pose_twist(
    odometry: Sequence[TimedOdom],
) -> Tuple[List[TimedOdom], List[TimedTwist]]:
    """Central-difference a real pose trajectory into measured velocity.

    TUM's Pioneer bags carry mocap-backed Odometry poses but no command topic
    and leave the message's twist fields at zero. The derivative is therefore
    an explicit measured quantity, not invented control input.
    """
    if len(odometry) < 2:
        return list(odometry), []
    derived_odometry: List[TimedOdom] = []
    derived_twists: List[TimedTwist] = []
    for index, sample in enumerate(odometry):
        before = odometry[max(0, index - 1)]
        after = odometry[min(len(odometry) - 1, index + 1)]
        dt = (after.offset_ns - before.offset_ns) / 1_000_000_000
        if dt <= 0:
            linear_x = angular_z = 0.0
        else:
            dx = after.x - before.x
            dy = after.y - before.y
            linear_x = (dx * math.cos(sample.yaw) + dy * math.sin(sample.yaw)) / dt
            angular_z = _angle_delta(after.yaw, before.yaw) / dt
        derived = TimedOdom(
            sample.offset_ns,
            sample.x,
            sample.y,
            sample.yaw,
            linear_x,
            angular_z,
        )
        derived_odometry.append(derived)
        if index % 2 == 0:  # source pose is sampled at 20 Hz; Twist is 10 Hz.
            derived_twists.append(
                TimedTwist(
                    sample.offset_ns,
                    linear_x,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    angular_z,
                )
            )
    return derived_odometry, derived_twists


def _tum_specs(*, imperfect: bool) -> Tuple[TopicSpec, ...]:
    specs = [
        TopicSpec(TOPIC_TASK, "std_msgs/msg/String", 0.0, ""),
        TopicSpec(TOPIC_IMAGE, "sensor_msgs/msg/CompressedImage", 5.0, "camera_link"),
        TopicSpec(TOPIC_MEASURED_TWIST, "geometry_msgs/msg/Twist", 10.0, ""),
        TopicSpec(TOPIC_ODOM, "nav_msgs/msg/Odometry", 20.0, "odom"),
        TopicSpec(TOPIC_TF, "tf2_msgs/msg/TFMessage", 20.0, "odom"),
        TopicSpec(TOPIC_SCAN, "sensor_msgs/msg/LaserScan", 5.0, "laser_link"),
    ]
    if not imperfect:
        specs.extend(
            [
                TopicSpec(TOPIC_TF_STATIC, "tf2_msgs/msg/TFMessage", 0.0, "base_link"),
                TopicSpec(TOPIC_CAMERA_INFO, "sensor_msgs/msg/CameraInfo", 0.0, "camera_link"),
            ]
        )
    return tuple(specs)


def _jpeg_imperfect(jpeg: bytes, *, freeze: Optional[bytes]) -> bytes:
    if freeze is not None:
        return freeze
    image = Image.open(io.BytesIO(jpeg)).convert("RGB").filter(ImageFilter.GaussianBlur(5.0))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=60, optimize=True)
    return out.getvalue()


def _write_tum_bag(path: Path, clip: TumClip, *, imperfect: bool) -> Dict[str, object]:
    start_ns = DEFAULT_START_TIME_NS
    specs = _tum_specs(imperfect=imperfect)
    order = {spec.topic: index for index, spec in enumerate(specs)}
    pending: List[Tuple[int, int, str, bytes]] = []

    def emit(topic: str, timestamp: int, payload: bytes) -> None:
        pending.append((timestamp, order[topic], topic, payload))

    emit(
        TOPIC_TASK,
        start_ns,
        M.serialize(M.make_string("" if imperfect else "map laboratory corridor"), "std_msgs/msg/String"),
    )
    if not imperfect:
        emit(
            TOPIC_TF_STATIC,
            start_ns,
            M.serialize(M.make_tf_static(start_ns, "base_link", "camera_link"), "tf2_msgs/msg/TFMessage"),
        )
        calibration = clip.calibration
        emit(
            TOPIC_CAMERA_INFO,
            start_ns,
            M.serialize(
                M.make_camera_info(
                    start_ns,
                    "camera_link",
                    calibration.width,
                    calibration.height,
                    k=calibration.k,
                    d=calibration.d,
                    distortion_model=calibration.distortion_model,
                ),
                "sensor_msgs/msg/CameraInfo",
            ),
        )

    frozen: Optional[bytes] = None
    image_written = 0
    image_dropped = 0
    for index, sample in enumerate(clip.images):
        seconds = sample.offset_ns / 1_000_000_000
        if imperfect and 2.0 <= seconds < 5.0:
            image_dropped += 1
            continue
        jpeg = sample.jpeg
        if imperfect and 5.0 <= seconds < 7.0:
            if frozen is None:
                frozen = _jpeg_imperfect(jpeg, freeze=None)
            jpeg = _jpeg_imperfect(jpeg, freeze=frozen)
        log_time = start_ns + sample.offset_ns
        header_time = log_time
        if imperfect:
            header_time += 850_000_000 if index % 2 else -850_000_000
        emit(
            TOPIC_IMAGE,
            log_time,
            M.serialize(
                M.make_compressed_image(header_time, "camera_link", jpeg),
                "sensor_msgs/msg/CompressedImage",
            ),
        )
        image_written += 1

    for sample in clip.twists:
        emit(
            TOPIC_MEASURED_TWIST,
            start_ns + sample.offset_ns,
            M.serialize(
                M.make_twist(sample.linear_x, sample.angular_z),
                "geometry_msgs/msg/Twist",
            ),
        )
    tf_dropped = 0
    for sample in clip.odometry:
        timestamp = start_ns + sample.offset_ns
        emit(
            TOPIC_ODOM,
            timestamp,
            M.serialize(
                M.make_odometry(
                    timestamp,
                    "odom",
                    "base_link",
                    sample.x,
                    sample.y,
                    sample.yaw,
                    sample.linear_x,
                    sample.angular_z,
                ),
                "nav_msgs/msg/Odometry",
            ),
        )
        seconds = sample.offset_ns / 1_000_000_000
        if imperfect and 2.0 <= seconds < 6.0:
            tf_dropped += 1
        else:
            emit(
                TOPIC_TF,
                timestamp,
                M.serialize(
                    M.make_tf(timestamp, "odom", "base_link", sample.x, sample.y, sample.yaw),
                    "tf2_msgs/msg/TFMessage",
                ),
            )
    scan_dropped = 0
    for sample in clip.scans:
        seconds = sample.offset_ns / 1_000_000_000
        if imperfect and 2.0 <= seconds < 5.0:
            scan_dropped += 1
            continue
        timestamp = start_ns + sample.offset_ns
        emit(
            TOPIC_SCAN,
            timestamp,
            M.serialize(
                M.make_laser_scan(
                    timestamp,
                    "laser_link",
                    angle_min=sample.angle_min,
                    angle_max=sample.angle_max,
                    angle_increment=sample.angle_increment,
                    time_increment=sample.time_increment,
                    scan_time=sample.scan_time,
                    range_min=sample.range_min,
                    range_max=sample.range_max,
                    ranges=sample.ranges,
                    intensities=sample.intensities,
                ),
                "sensor_msgs/msg/LaserScan",
            ),
        )
    _write_messages(path, specs, pending, library="example_data/TUM-v1")
    return {
        "message_count": len(pending),
        "topic_counts": _topic_counts(pending),
        "source_window_start_ns": clip.source_start_ns,
        "source_window_duration_seconds": clip.duration_ns / 1_000_000_000,
        "source_topics": dict(clip.source_topics),
        "real_angular_z_min": min(sample.angular_z for sample in clip.twists),
        "real_angular_z_max": max(sample.angular_z for sample in clip.twists),
        "injections": (
            {
                "unlabeled_marker": True,
                "missing_camera_info": True,
                "missing_static_tf": True,
                "camera_header_jitter_ms": 850,
                "camera_frames_dropped": image_dropped,
                "frozen_blurred_camera_window_seconds": [5.0, 7.0],
                "dynamic_tf_messages_dropped": tf_dropped,
                "laser_messages_dropped": scan_dropped,
                "expected_qa_classes": ["defect", "risk"],
            }
            if imperfect
            else None
        ),
        "image_messages_written": image_written,
    }


def _projection(action: Sequence[float], previous: Optional[Sequence[float]], dt: float) -> Tuple[float, float]:
    if previous is None or dt <= 0:
        return 0.0, 0.0
    left = (float(action[0]) - float(previous[0])) / dt
    right = (float(action[7]) - float(previous[7])) / dt
    return ((left + right) / 2.0, (right - left) / 2.0)


def _write_aloha_bag(path: Path, parquet_path: Path) -> Dict[str, object]:
    table = pq.read_table(
        parquet_path,
        columns=[
            "observation.state",
            "observation.effort",
            "action",
            "episode_index",
            "frame_index",
            "timestamp",
        ],
        filters=[("episode_index", "=", 0)],
    )
    rows = [row for row in table.to_pylist() if float(row["timestamp"]) < 8.0]
    rows = rows[::2]  # 25 Hz, preserving a real contiguous episode excerpt.
    if len(rows) < 100:
        raise RuntimeError(f"ALOHA excerpt unexpectedly short: {len(rows)} rows")
    specs = (
        TopicSpec(TOPIC_TASK, "std_msgs/msg/String", 0.0, ""),
        TopicSpec("/observation/joint_states", "sensor_msgs/msg/JointState", 25.0, "aloha_base"),
        TopicSpec("/action/joint_targets", "sensor_msgs/msg/JointState", 25.0, "aloha_base"),
        TopicSpec("/action/twist_projection", "geometry_msgs/msg/Twist", 25.0, ""),
        TopicSpec("/derived/action_projection/odom", "nav_msgs/msg/Odometry", 25.0, "aloha_action_projection"),
        TopicSpec(TOPIC_TF, "tf2_msgs/msg/TFMessage", 25.0, "aloha_action_projection"),
    )
    order = {spec.topic: index for index, spec in enumerate(specs)}
    start_ns = DEFAULT_START_TIME_NS + 20_000_000_000
    pending: List[Tuple[int, int, str, bytes]] = []

    def emit(topic: str, timestamp: int, payload: bytes) -> None:
        pending.append((timestamp, order[topic], topic, payload))

    emit(
        TOPIC_TASK,
        start_ns,
        M.serialize(M.make_string("prepare coffee with bimanual ALOHA"), "std_msgs/msg/String"),
    )
    previous_action: Optional[Sequence[float]] = None
    previous_time: Optional[float] = None
    x = y = yaw = 0.0
    max_abs_angular = 0.0
    for row in rows:
        rel = float(row["timestamp"])
        timestamp = start_ns + int(round(rel * 1_000_000_000))
        state = [float(value) for value in row["observation.state"]]
        effort = [float(value) for value in row["observation.effort"]]
        action = [float(value) for value in row["action"]]
        dt = rel - previous_time if previous_time is not None else 0.0
        linear, angular = _projection(action, previous_action, dt)
        max_abs_angular = max(max_abs_angular, abs(angular))
        if dt > 0:
            yaw += angular * dt
            x += linear * math.cos(yaw) * dt
            y += linear * math.sin(yaw) * dt
        emit(
            "/observation/joint_states",
            timestamp,
            M.serialize(
                M.make_joint_state(
                    timestamp,
                    "aloha_base",
                    state,
                    (),
                    names=ALOHA_JOINT_NAMES,
                    efforts=effort,
                ),
                "sensor_msgs/msg/JointState",
            ),
        )
        emit(
            "/action/joint_targets",
            timestamp,
            M.serialize(
                M.make_joint_state(
                    timestamp,
                    "aloha_base",
                    action,
                    (),
                    names=ALOHA_JOINT_NAMES,
                ),
                "sensor_msgs/msg/JointState",
            ),
        )
        emit(
            "/action/twist_projection",
            timestamp,
            M.serialize(M.make_twist(linear, angular), "geometry_msgs/msg/Twist"),
        )
        emit(
            "/derived/action_projection/odom",
            timestamp,
            M.serialize(
                M.make_odometry(
                    timestamp,
                    "aloha_action_projection",
                    "aloha_action_state",
                    x,
                    y,
                    yaw,
                    linear,
                    angular,
                ),
                "nav_msgs/msg/Odometry",
            ),
        )
        emit(
            TOPIC_TF,
            timestamp,
            M.serialize(
                M.make_tf(
                    timestamp,
                    "aloha_action_projection",
                    "aloha_action_state",
                    x,
                    y,
                    yaw,
                ),
                "tf2_msgs/msg/TFMessage",
            ),
        )
        previous_action, previous_time = action, rel
    _write_messages(path, specs, pending, library="example_data/ALOHA-v1")
    return {
        "message_count": len(pending),
        "topic_counts": _topic_counts(pending),
        "source_episode_index": 0,
        "source_rows": len(rows),
        "source_fps": 50,
        "output_fps": 25,
        "joint_dimensions": len(ALOHA_JOINT_NAMES),
        "max_abs_projected_angular_z": max_abs_angular,
        "projection": {
            "linear_x": "d(left_waist + right_waist) / (2*dt)",
            "angular_z": "d(right_waist - left_waist) / (2*dt)",
            "warning": (
                "This namespaced Twist/Odometry pair is an explicit action-space "
                "projection for current AMR-oriented QA/provenance, not mobile-base motion. "
                "The complete 14-D source state/action is retained on JointState topics."
            ),
        },
    }


def _synthetic_plate(width: int = 190, height: int = 58) -> Image.Image:
    image = Image.new("RGB", (width, height), (245, 245, 235))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, width - 3, height - 3), radius=7, outline=(20, 30, 50), width=4)
    draw.rectangle((10, 8, 29, height - 9), fill=(35, 85, 175))
    font = ImageFont.load_default(size=22)
    draw.text((38, 16), "SYN 2026", fill=(15, 20, 30), font=font)
    return image


def _composite_pii(jpeg: bytes, face: Image.Image) -> bytes:
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    portrait = face.convert("RGB").resize((150, 150), Image.Resampling.LANCZOS)
    image.paste(portrait, (24, 28))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 24, 178, 182), outline=(255, 230, 40), width=4)
    plate = _synthetic_plate()
    image.paste(plate, (image.width - plate.width - 24, image.height - plate.height - 28))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


def _write_pii_bag(path: Path, face_path: Path) -> Dict[str, object]:
    if not face_path.is_file():
        raise FileNotFoundError(f"synthetic face asset missing: {face_path}")
    face = Image.open(face_path).convert("RGB")
    with tempfile.TemporaryDirectory(prefix="robot-vault-pii-base-") as tmp:
        base = generate_sim(
            tmp,
            episodes=1,
            duration=8.0,
            seed=20260810,
            robot_id="example-privacy-01",
            image_width=OUTPUT_WIDTH,
            image_height=OUTPUT_HEIGHT,
            jpeg_quality=JPEG_QUALITY,
            filename="pii-base.mcap",
        )
        specs: List[TopicSpec] = []
        pending: List[Tuple[int, int, str, bytes]] = []
        topic_order: Dict[str, int] = {}
        image_count = 0
        with base.mcap_path.open("rb") as handle:
            reader = make_reader(handle)
            for schema, channel, message in reader.iter_messages(log_time_order=True):
                if channel.topic not in topic_order:
                    topic_order[channel.topic] = len(specs)
                    specs.append(TopicSpec(channel.topic, schema.name, 0.0, ""))
                payload = bytes(message.data)
                if channel.topic == "/camera/image_compressed":
                    decoded = M.TYPESTORE.deserialize_cdr(payload, schema.name)
                    payload = M.serialize(
                        M.make_compressed_image(
                            message.log_time,
                            _frame_id(decoded, "camera_link"),
                            _composite_pii(bytes(decoded.data), face),
                        ),
                        schema.name,
                    )
                    image_count += 1
                elif channel.topic == TOPIC_TASK:
                    payload = M.serialize(
                        M.make_string("inspect synthetic privacy targets"),
                        "std_msgs/msg/String",
                    )
                pending.append(
                    (message.log_time, topic_order[channel.topic], channel.topic, payload)
                )
        _write_messages(path, tuple(specs), pending, library="example_data/synthetic-pii-v1")
    if image_count < 20:
        raise RuntimeError(f"synthetic PII fixture has too few camera frames: {image_count}")
    return {
        "message_count": len(pending),
        "topic_counts": _topic_counts(pending),
        "synthetic_camera_frames": image_count,
        "synthetic_face_asset_sha256": _sha256(face_path),
        "synthetic_plate_text": "SYN 2026",
        "privacy_claim": (
            "All depicted identity and plate targets are synthetic. No real person, "
            "vehicle, customer, or bystander image is present."
        ),
    }


def _write_messages(
    path: Path,
    specs: Sequence[TopicSpec],
    pending: Sequence[Tuple[int, int, str, bytes]],
    *,
    library: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    by_topic = {spec.topic: spec for spec in specs}
    with path.open("wb") as handle:
        writer = McapWriter(handle, CompressionType.ZSTD)
        # McapWriter records the acceptance library string internally. The
        # source-specific library remains in the sidecar and MCAP channel set.
        channels = {topic: writer.channel_for(spec) for topic, spec in by_topic.items()}
        for timestamp, order, topic, payload in sorted(pending, key=lambda row: (row[0], row[1])):
            del order
            writer.add(channels[topic], timestamp, payload)
        writer.finish()


def _topic_counts(pending: Iterable[Tuple[int, int, str, bytes]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for _timestamp, _order, topic, _payload in pending:
        counts[topic] = counts.get(topic, 0) + 1
    return dict(sorted(counts.items()))


def _inventory(path: Path) -> Dict[str, object]:
    counts: Dict[str, int] = {}
    schemas: Dict[str, str] = {}
    messages = 0
    with path.open("rb") as handle:
        reader = make_reader(handle)
        for schema, channel, _message in reader.iter_messages(log_time_order=True):
            messages += 1
            counts[channel.topic] = counts.get(channel.topic, 0) + 1
            schemas[channel.topic] = schema.name
    if messages == 0:
        raise RuntimeError(f"generated empty MCAP: {path}")
    return {
        "message_count": messages,
        "topic_counts": dict(sorted(counts.items())),
        "schemas": dict(sorted(schemas.items())),
    }


def _write_sidecar_and_datacard(
    path: Path,
    *,
    label: str,
    robot_id: str,
    robot_type: str,
    demonstrates: Sequence[str],
    look_at: Sequence[str],
    source: Mapping[str, object],
    transformations: Sequence[str],
    notes: Sequence[str],
    generation: Mapping[str, object],
) -> Dict[str, object]:
    inventory = _inventory(path)
    digest = _sha256(path)
    sidecar = {
        "schema": "robot-vault-example/v1",
        "example_data_version": EXAMPLE_DATA_VERSION,
        "label": label,
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest,
        "robot_id": robot_id,
        "robot_type": robot_type,
        "demonstrates": list(demonstrates),
        "look_at": list(look_at),
        "source": dict(source),
        "transformations": list(transformations),
        "notes": list(notes),
        "generation": dict(generation),
        **inventory,
    }
    sidecar_path = path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    source_lines = "\n".join(
        f"- **{key.replace('_', ' ').title()}:** {value}"
        for key, value in source.items()
        if value is not None
    )
    datacard = f"""# {label} example datacard

- **Robot ID:** `{robot_id}`
- **Robot type:** {robot_type}
- **MCAP:** `{path.name}` ({path.stat().st_size:,} bytes)
- **SHA-256:** `{digest}`
- **Encoding:** ROS 2 CDR payloads with `ros2msg` schemas in MCAP

## What it demonstrates

""" + "\n".join(f"- {item}" for item in demonstrates) + f"""

## What to inspect

""" + "\n".join(f"- {item}" for item in look_at) + f"""

## Source, license, and attribution

{source_lines}

## Transformations

""" + "\n".join(f"- {item}" for item in transformations) + f"""

## Notes and limitations

""" + "\n".join(f"- {item}" for item in notes) + "\n"
    path.with_suffix(".datacard.md").write_text(datacard)
    return sidecar


def generate_examples(
    *,
    out_dir: Path,
    source_dir: Path,
    tum_bag: Path,
    aloha_parquet: Path,
    synthetic_face: Path,
    download: bool = True,
    tum_start_seconds: Optional[float] = None,
    duration_seconds: float = 8.0,
) -> Dict[str, object]:
    if duration_seconds < 6.0:
        raise ValueError("duration_seconds must be >= 6 so QA injection windows are observable")
    source_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    tum_sha = _ensure_source(tum_bag, url=TUM_URL, download=download)
    aloha_sha = _ensure_source(
        aloha_parquet,
        url=ALOHA_URL,
        download=download,
        expected_sha256=ALOHA_EXPECTED_SHA256,
    )
    clip = read_tum_clip(
        tum_bag,
        duration_seconds=duration_seconds,
        start_seconds=tum_start_seconds,
    )
    bags: List[Dict[str, object]] = []

    wheeled_path = out_dir / "wheeled-nominal.mcap"
    wheeled_generation = _write_tum_bag(wheeled_path, clip, imperfect=False)
    bags.append(
        _write_sidecar_and_datacard(
            wheeled_path,
            label="wheeled-nominal",
            robot_id="example-wheeled-01",
            robot_type="Pioneer mobile robot",
            demonstrates=(
                "clean nominal upload -> episode -> QA path",
                "real camera, laser, wheel odometry, and measured Twist data",
                "real non-zero angular_z preserved in the typed v2 Twist family",
            ),
            look_at=(
                "Episodes QA: strongest clean example",
                "Explore: odometry, laser_scan, compressed_image, and twist families",
                "Datasets: use this episode for the pre-populated signed example cut",
            ),
            source={
                "dataset": "TUM RGB-D Benchmark",
                "sequence": "fr2/pioneer_slam2",
                "source_url": TUM_DOWNLOAD_URL,
                "license": TUM_LICENSE,
                "attribution": TUM_ATTRIBUTION,
                "source_sha256": tum_sha,
            },
            transformations=(
                "selected one contiguous eight-second window containing a real Pioneer turn",
                "derived measured linear/angular velocity from the real mocap-backed /pose trajectory because the source carries no command topic",
                "rebased timestamps to a deterministic epoch without changing relative timing",
                "decoded ROS 1 messages and re-encoded equivalent ROS 2 CDR messages in MCAP",
                "sampled RGB at 5 Hz and resized to 480x360; scaled source intrinsics accordingly",
            ),
            notes=(
                "CC BY 4.0 attribution must remain with this derivative.",
                "The QA calibration check proves artifact presence, not calibration accuracy.",
            ),
            generation=wheeled_generation,
        )
    )

    manipulator_path = out_dir / "manipulator-pickplace.mcap"
    manipulator_generation = _write_aloha_bag(manipulator_path, aloha_parquet)
    bags.append(
        _write_sidecar_and_datacard(
            manipulator_path,
            label="manipulator-pickplace",
            robot_id="example-manipulator-01",
            robot_type="bimanual ALOHA manipulator",
            demonstrates=(
                "LeRobot-native Parquet -> MCAP ingest -> LeRobot export round-trip provenance",
                "full 14-D observed joint state, effort, and action target streams",
                "typed Twist/action provenance and the recorded-but-excluded manipulator-shaped QA subscores",
            ),
            look_at=(
                "Explore: observation and action JointState topics",
                "Episodes QA detail: commodity excluded_subscores remains explicit",
                "Sidecar projection warning before interpreting Twist/Odometry as mobile motion",
            ),
            source={
                "dataset": "LeRobot aloha_static_coffee",
                "source_url": ALOHA_CARD_URL,
                "revision": ALOHA_REVISION,
                "license": ALOHA_LICENSE,
                "attribution": ALOHA_ATTRIBUTION,
                "source_sha256": aloha_sha,
            },
            transformations=(
                "selected the first eight seconds of source episode 0 and sampled 50 Hz rows at 25 Hz",
                "preserved all 14 source state/action dimensions as sensor_msgs/JointState",
                "added a namespaced, reversible two-waist-velocity projection into Twist/Odometry for current AMR-oriented metrics",
            ),
            notes=(
                "The projection is not mobile-base motion and is never described as such.",
                "No source video is redistributed in this compact excerpt; camera fields are absent by design.",
            ),
            generation=manipulator_generation,
        )
    )

    imperfect_path = out_dir / "imperfect-run.mcap"
    imperfect_generation = _write_tum_bag(imperfect_path, clip, imperfect=True)
    bags.append(
        _write_sidecar_and_datacard(
            imperfect_path,
            label="imperfect-run",
            robot_id="example-wheeled-01",
            robot_type="Pioneer mobile robot (engineered QA-fault derivative)",
            demonstrates=(
                "real QA DEFECT signals from missing calibration, timestamp jitter, and stream gaps",
                "real QA RISK signals from an unlabeled episode and frozen/blurred frames",
                "checks flag data without deleting it",
            ),
            look_at=(
                "Episodes QA breakdown: both DEFECT and RISK classes",
                "cross_stream_sync interpolation provenance",
                "calibration, completeness, image_quality, and labels reasons",
            ),
            source={
                "dataset": "TUM RGB-D Benchmark derivative",
                "sequence": "fr2/pioneer_slam2",
                "source_url": TUM_DOWNLOAD_URL,
                "license": TUM_LICENSE,
                "attribution": TUM_ATTRIBUTION,
                "source_sha256": tum_sha,
            },
            transformations=(
                "started from the exact wheeled-nominal source window",
                "removed CameraInfo and static TF, dropped camera/laser/TF stretches, jittered camera header stamps by +/-850 ms",
                "froze and blurred a camera interval and emitted a deliberately empty task marker",
                "did not insert, update, or otherwise fabricate any qa_reports row",
            ),
            notes=(
                "This bag must be accepted only if the real processor reports at least one low DEFECT and one low RISK check.",
                "Malformed/truncated CDR was intentionally avoided so every retained message remains decodable.",
            ),
            generation=imperfect_generation,
        )
    )

    pii_path = out_dir / "pii-bearing.mcap"
    pii_generation = _write_pii_bag(pii_path, synthetic_face)
    bags.append(
        _write_sidecar_and_datacard(
            pii_path,
            label="pii-bearing",
            robot_id="example-wheeled-01",
            robot_type="fully synthetic AMR privacy fixture",
            demonstrates=(
                "face/license-plate scrubbing (Pro) with pii_status=scrubbed when a detector runs",
                "honest `pii_status=raw` when no detector runs; exports default closed",
                "the same bag makes both deployment paths observable",
            ),
            look_at=(
                "processor media metadata: pii_backend, pii_status, and regions blurred",
                "export gate behavior with and without detector-earned scrubbed status",
                "synthetic portrait and fictional plate visible in every camera frame before scrubbing",
            ),
            source={
                "dataset": "Robot Vault deterministic sim_gen acceptance fixture plus generated fictional portrait",
                "source_url": "development/sim_gen and examples/assets/ASSET_PROVENANCE.md",
                "license": "project-owned synthetic fixture",
                "attribution": "Robot Vault example-data contributors; no external person or vehicle imagery",
                "source_sha256": _sha256(synthetic_face),
            },
            transformations=(
                "generated one deterministic eight-second sim_gen episode",
                "composited one fictional generated face and a deterministic fictional SYN 2026 plate into each synthetic frame",
                "preserved all non-camera acceptance-fixture messages byte-for-byte",
            ),
            notes=(
                "SYNTHETIC ONLY: no real face, license plate, customer, driver, or bystander data is present.",
                "A permissive copyright license for real road imagery would not clear privacy, biometric, publicity, GDPR, or BIPA rights; real dashcam imagery is deliberately excluded.",
                "pii_status proves which backend ran over every encoded frame; it is not a claim that any detector is infallible.",
            ),
            generation=pii_generation,
        )
    )

    manifest = {
        "schema": "robot-vault-example-bundle/v1",
        "example_data_version": EXAMPLE_DATA_VERSION,
        "generator": "PYTHONPATH=development python3 -m example_data",
        "bags": [
            {
                "label": bag["label"],
                "file": bag["file"],
                "bytes": bag["bytes"],
                "sha256": bag["sha256"],
                "message_count": bag["message_count"],
                "sidecar": Path(bag["file"]).with_suffix(".json").name,
                "datacard": Path(bag["file"]).with_suffix(".datacard.md").name,
            }
            for bag in bags
        ],
        "total_bytes": sum(int(bag["bytes"]) for bag in bags),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
