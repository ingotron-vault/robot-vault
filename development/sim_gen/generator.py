"""Deterministic simulation MCAP generator for AMR episodes.

Emits ONE `.mcap` file holding N episodes separated by >10 s of message
silence, so the gap-based episode segmenter
(`EPISODE_GAP_SECONDS=5`) splits them cleanly, plus a sidecar `.sim.json`
recording exactly what was generated -- including what was deliberately broken
in the `--bad-episode`, which is the ground truth every later QA test asserts
against.

Determinism: nothing reads the wall clock or the OS entropy pool. Timestamps
start from a fixed epoch (`DEFAULT_START_TIME_NS`, overridable) and every
random draw comes from `numpy.random.default_rng([seed, ...])`, so the same
arguments produce byte-identical output in a given image.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from mcap.writer import CompressionType, Writer

from . import messages as M
from .motion import MotionModel
from .scene import DEFAULT_HEIGHT, DEFAULT_WIDTH, encode_jpeg, render_rgb

SIM_GEN_VERSION = "1.0.0"
#: Bump when the sidecar layout changes in a way consumers must notice.
SIDECAR_VERSION = 1

#: 2026-01-01T00:00:00Z. A fixed epoch, not `time.time()`, so two runs with the
#: same seed are byte-identical.
DEFAULT_START_TIME_NS = 1_767_225_600_000_000_000
DEFAULT_EPISODE_GAP_SECONDS = 12.0
DEFAULT_DURATION_SECONDS = 20.0
DEFAULT_ROBOT_ID = "sim-amr-01"
DEFAULT_JPEG_QUALITY = 72
DEFAULT_SEED = 0

COMPRESSION_CHOICES = {
    "none": CompressionType.NONE,
    "lz4": CompressionType.LZ4,
    "zstd": CompressionType.ZSTD,
}


@dataclass(frozen=True)
class TopicSpec:
    topic: str
    msgtype: str
    #: 0.0 means "once per episode, at the episode start".
    rate_hz: float
    frame_id: str


TOPIC_TASK = "/session/task"
TOPIC_TF_STATIC = "/tf_static"
TOPIC_IMAGE = "/camera/image_compressed"
TOPIC_CAMERA_INFO = "/camera/camera_info"
TOPIC_CMD_VEL = "/cmd_vel"
TOPIC_ODOM = "/odom"
TOPIC_JOINT_STATES = "/joint_states"
TOPIC_TF = "/tf"
TOPIC_IMU = "/imu/data"
TOPIC_GPS = "/gps/fix"

#: Declaration order doubles as the tie-break for messages sharing a log_time,
#: which is what keeps the written byte stream stable across runs.
TOPICS: Tuple[TopicSpec, ...] = (
    TopicSpec(TOPIC_TASK, "std_msgs/msg/String", 0.0, ""),
    TopicSpec(TOPIC_TF_STATIC, "tf2_msgs/msg/TFMessage", 0.0, "base_link"),
    TopicSpec(TOPIC_IMAGE, "sensor_msgs/msg/CompressedImage", 10.0, "camera_link"),
    TopicSpec(TOPIC_CAMERA_INFO, "sensor_msgs/msg/CameraInfo", 10.0, "camera_link"),
    TopicSpec(TOPIC_CMD_VEL, "geometry_msgs/msg/Twist", 10.0, ""),
    TopicSpec(TOPIC_ODOM, "nav_msgs/msg/Odometry", 20.0, "odom"),
    TopicSpec(TOPIC_JOINT_STATES, "sensor_msgs/msg/JointState", 20.0, "base_link"),
    TopicSpec(TOPIC_TF, "tf2_msgs/msg/TFMessage", 20.0, "odom"),
    TopicSpec(TOPIC_IMU, "sensor_msgs/msg/Imu", 50.0, "imu_link"),
    TopicSpec(TOPIC_GPS, "sensor_msgs/msg/NavSatFix", 5.0, "gps_link"),
)

#: Per-episode task labels -- the marker episode segmentation consumes and
#: LeRobot v3 requires. Deterministic, and varied across episodes.
TASK_LABELS: Tuple[str, ...] = (
    "navigate to dock A",
    "deliver tote to station 2",
    "patrol aisle 3",
    "return to charger",
    "transport pallet to bay 4",
    "inspect aisle 7",
    "navigate to dock B",
    "stage cart at outbound lane",
)

# --- deliberately-bad-episode knobs -----------------------------------------
IMU_DROPOUT_SECONDS = 5.0
FROZEN_CAMERA_SECONDS = 3.0
BLUR_RADIUS_PX = 4.0


def _clip_window(lo: float, hi: float, duration: float) -> Optional[Tuple[float, float]]:
    lo = max(0.0, min(lo, duration))
    hi = max(0.0, min(hi, duration))
    return (lo, hi) if hi > lo else None


def degradation_windows(duration: float) -> Dict[str, Optional[Tuple[float, float]]]:
    """Episode-relative [start, end) windows for each deliberate defect.

    Laid out so the four defects barely overlap at the default durations
    (15-20 s): only `camera_blur` and `stationary` share time, and those two
    are independent signals (image sharpness vs. cmd_vel/odom agreement).
    Short episodes clip; an emptied window means that defect is not applied
    and not recorded in the sidecar.
    """
    return {
        "imu_dropout": _clip_window(
            0.10 * duration, 0.10 * duration + IMU_DROPOUT_SECONDS, duration
        ),
        "camera_frozen": _clip_window(
            0.50 * duration, 0.50 * duration + FROZEN_CAMERA_SECONDS, duration
        ),
        "camera_blur": _clip_window(0.72 * duration, 0.92 * duration, duration),
        "stationary": _clip_window(0.72 * duration, 0.95 * duration, duration),
    }


class McapWriter:
    """Thin wrapper: one `ros2msg` schema per type, one channel per topic.

    This is public because the shipped example-data converter deliberately
    reuses the acceptance fixture's writer and message definitions.  Keeping
    one writer avoids a second, subtly different MCAP construction path.
    """

    def __init__(self, fileobj, compression: CompressionType) -> None:
        self._writer = Writer(fileobj, compression=compression)
        self._writer.start(profile="ros2", library=f"sim_gen {SIM_GEN_VERSION}")
        self._schema_ids: Dict[str, int] = {}
        self._channel_ids: Dict[str, int] = {}
        self._sequence: Dict[int, int] = {}

    def channel_for(self, spec: TopicSpec) -> int:
        if spec.topic in self._channel_ids:
            return self._channel_ids[spec.topic]
        if spec.msgtype not in self._schema_ids:
            self._schema_ids[spec.msgtype] = self._writer.register_schema(
                name=spec.msgtype,
                encoding="ros2msg",
                data=M.message_definition(spec.msgtype).encode("utf-8"),
            )
        channel_id = self._writer.register_channel(
            schema_id=self._schema_ids[spec.msgtype],
            topic=spec.topic,
            message_encoding="cdr",
            metadata={},
        )
        self._channel_ids[spec.topic] = channel_id
        self._sequence[channel_id] = 0
        return channel_id

    def add(self, channel_id: int, log_time_ns: int, payload: bytes) -> None:
        seq = self._sequence[channel_id]
        self._sequence[channel_id] = seq + 1
        self._writer.add_message(
            channel_id=channel_id,
            log_time=log_time_ns,
            publish_time=log_time_ns,
            sequence=seq,
            data=payload,
        )

    def finish(self) -> None:
        self._writer.finish()


def _sample_times(rate_hz: float, duration: float) -> List[float]:
    """Episode-relative sample times, [0, duration) -- end-exclusive so the
    inter-episode silence is exactly `gap_seconds` plus one sample period."""
    n = int(round(duration * rate_hz))
    return [i / rate_hz for i in range(n)]


def _sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "robot"


def _in_window(t: float, window: Optional[Tuple[float, float]]) -> bool:
    return window is not None and window[0] <= t < window[1]


def _generate_episode(
    writer: McapWriter,
    channels: Dict[str, int],
    *,
    episode_index: int,
    episode_start_ns: int,
    duration: float,
    seed: int,
    image_width: int,
    image_height: int,
    jpeg_quality: int,
    is_bad: bool,
) -> dict:
    specs = {spec.topic: spec for spec in TOPICS}
    order = {spec.topic: i for i, spec in enumerate(TOPICS)}
    windows: Dict[str, Optional[Tuple[float, float]]] = (
        degradation_windows(duration)
        if is_bad
        else {
            "imu_dropout": None,
            "camera_frozen": None,
            "camera_blur": None,
            "stationary": None,
        }
    )

    motion = MotionModel(
        duration,
        np.random.default_rng([seed, episode_index, 1]),
        stationary_window=windows["stationary"],
    )
    gps_rng = np.random.default_rng([seed, episode_index, 2])

    pending: List[Tuple[int, int, int, int, bytes]] = []
    counts: Dict[str, int] = {}

    def emit(topic: str, t_rel: float, msg: object) -> None:
        spec = specs[topic]
        log_time = episode_start_ns + int(round(t_rel * 1e9))
        seq = counts.get(topic, 0)
        counts[topic] = seq + 1
        pending.append(
            (log_time, order[topic], seq, channels[topic], M.serialize(msg, spec.msgtype))
        )

    task_label = TASK_LABELS[(seed + episode_index) % len(TASK_LABELS)]

    # --- once per episode ---
    emit(TOPIC_TASK, 0.0, M.make_string(task_label))
    emit(
        TOPIC_TF_STATIC,
        0.0,
        M.make_tf_static(episode_start_ns, "base_link", "camera_link"),
    )

    # --- camera (10 Hz) ---
    frozen_jpeg: Optional[bytes] = None
    for i, t in enumerate(_sample_times(specs[TOPIC_IMAGE].rate_hz, duration)):
        idx = motion.index(t)
        if _in_window(t, windows["camera_frozen"]) and frozen_jpeg is not None:
            jpeg = frozen_jpeg
        else:
            rgb = render_rgb(
                image_width,
                image_height,
                float(motion.theta[idx]),
                float(motion.distance[idx]),
                np.random.default_rng([seed, episode_index, 3, i]),
            )
            blur = BLUR_RADIUS_PX if _in_window(t, windows["camera_blur"]) else None
            jpeg = encode_jpeg(rgb, jpeg_quality, blur)
            if _in_window(t, windows["camera_frozen"]):
                frozen_jpeg = jpeg
        emit(
            TOPIC_IMAGE,
            t,
            M.make_compressed_image(
                episode_start_ns + int(round(t * 1e9)),
                specs[TOPIC_IMAGE].frame_id,
                jpeg,
            ),
        )

    # --- camera_info (10 Hz; absent entirely in the bad episode) ---
    if not is_bad:
        for t in _sample_times(specs[TOPIC_CAMERA_INFO].rate_hz, duration):
            emit(
                TOPIC_CAMERA_INFO,
                t,
                M.make_camera_info(
                    episode_start_ns + int(round(t * 1e9)),
                    specs[TOPIC_CAMERA_INFO].frame_id,
                    image_width,
                    image_height,
                ),
            )

    # --- cmd_vel (10 Hz) -- always the *commanded* profile ---
    for t in _sample_times(specs[TOPIC_CMD_VEL].rate_hz, duration):
        idx = motion.index(t)
        emit(
            TOPIC_CMD_VEL,
            t,
            M.make_twist(float(motion.v_cmd[idx]), float(motion.w_cmd[idx])),
        )

    # --- odom / joint_states / tf (20 Hz) -- the *achieved* state ---
    for t in _sample_times(specs[TOPIC_ODOM].rate_hz, duration):
        idx = motion.index(t)
        stamp = episode_start_ns + int(round(t * 1e9))
        emit(
            TOPIC_ODOM,
            t,
            M.make_odometry(
                stamp,
                specs[TOPIC_ODOM].frame_id,
                "base_link",
                float(motion.x[idx]),
                float(motion.y[idx]),
                float(motion.theta[idx]),
                float(motion.v[idx]),
                float(motion.w[idx]),
            ),
        )
        emit(
            TOPIC_JOINT_STATES,
            t,
            M.make_joint_state(
                stamp,
                specs[TOPIC_JOINT_STATES].frame_id,
                (float(motion.wheel_pos_left[idx]), float(motion.wheel_pos_right[idx])),
                (float(motion.wheel_vel_left[idx]), float(motion.wheel_vel_right[idx])),
            ),
        )
        emit(
            TOPIC_TF,
            t,
            M.make_tf(
                stamp,
                specs[TOPIC_TF].frame_id,
                "base_link",
                float(motion.x[idx]),
                float(motion.y[idx]),
                float(motion.theta[idx]),
            ),
        )

    # --- imu (50 Hz, with the deliberate dropout) ---
    for t in _sample_times(specs[TOPIC_IMU].rate_hz, duration):
        if _in_window(t, windows["imu_dropout"]):
            continue
        idx = motion.index(t)
        emit(
            TOPIC_IMU,
            t,
            M.make_imu(
                episode_start_ns + int(round(t * 1e9)),
                specs[TOPIC_IMU].frame_id,
                float(motion.theta[idx]),
                (float(motion.gyro_x[idx]), float(motion.gyro_y[idx]), float(motion.gyro_z[idx])),
                (
                    float(motion.accel_x[idx]),
                    float(motion.accel_y[idx]),
                    float(motion.accel_z[idx]),
                ),
            ),
        )

    # --- gps (5 Hz) ---
    for i, t in enumerate(_sample_times(specs[TOPIC_GPS].rate_hz, duration)):
        idx = motion.index(t)
        drift = (
            1.2e-6 * np.sin(0.08 * t) + gps_rng.normal(0.0, 2.5e-7),
            9.0e-7 * np.cos(0.06 * t) + gps_rng.normal(0.0, 2.5e-7),
            0.35 * np.sin(0.05 * t) + gps_rng.normal(0.0, 0.05),
        )
        emit(
            TOPIC_GPS,
            t,
            M.make_navsatfix(
                episode_start_ns + int(round(t * 1e9)),
                specs[TOPIC_GPS].frame_id,
                float(motion.x[idx]),
                float(motion.y[idx]),
                (float(drift[0]), float(drift[1]), float(drift[2])),
            ),
        )

    pending.sort(key=lambda row: (row[0], row[1], row[2]))
    for log_time, _order, _seq, channel_id, payload in pending:
        writer.add(channel_id, log_time, payload)

    return {
        "index": episode_index,
        "task_label": task_label,
        "start_time_ns": pending[0][0],
        "end_time_ns": pending[-1][0],
        "duration_seconds": duration,
        "degraded": bool(is_bad),
        "degradations": _degradation_records(windows) if is_bad else [],
        "message_count": len(pending),
        "message_counts": {topic: counts.get(topic, 0) for topic in counts},
        "topics": sorted(counts),
    }


def _degradation_records(
    windows: Dict[str, Optional[Tuple[float, float]]]
) -> List[dict]:
    """Ground truth for the QA tests: what was broken, where, and how."""
    records: List[dict] = []
    imu = windows["imu_dropout"]
    if imu:
        records.append(
            {
                "kind": "imu_dropout",
                "topics": [TOPIC_IMU],
                "start_offset_s": imu[0],
                "end_offset_s": imu[1],
                "detail": (
                    "no sensor_msgs/msg/Imu messages published in this window "
                    "(a message gap, while every other topic keeps publishing)"
                ),
            }
        )
    frozen = windows["camera_frozen"]
    if frozen:
        records.append(
            {
                "kind": "camera_frozen",
                "topics": [TOPIC_IMAGE],
                "start_offset_s": frozen[0],
                "end_offset_s": frozen[1],
                "detail": (
                    "every CompressedImage in this window carries byte-identical "
                    "JPEG payloads (a stuck camera), while the robot keeps moving"
                ),
            }
        )
    blur = windows["camera_blur"]
    if blur:
        records.append(
            {
                "kind": "camera_blur",
                "topics": [TOPIC_IMAGE],
                "start_offset_s": blur[0],
                "end_offset_s": blur[1],
                "params": {"gaussian_blur_radius_px": BLUR_RADIUS_PX},
                "detail": "frames Gaussian-blurred before JPEG encoding",
            }
        )
    stationary = windows["stationary"]
    if stationary:
        records.append(
            {
                "kind": "kinematic_implausibility",
                "topics": [TOPIC_CMD_VEL, TOPIC_ODOM, TOPIC_JOINT_STATES, TOPIC_TF],
                "start_offset_s": stationary[0],
                "end_offset_s": stationary[1],
                "detail": (
                    "cmd_vel keeps commanding motion while odom/joint_states/tf "
                    "report zero velocity and a frozen pose (commanded-but-stationary)"
                ),
            }
        )
    records.append(
        {
            "kind": "missing_camera_info",
            "topics": [TOPIC_CAMERA_INFO],
            "start_offset_s": 0.0,
            "end_offset_s": None,
            "detail": (
                "no sensor_msgs/msg/CameraInfo messages at all for this episode "
                "(calibration incompleteness)"
            ),
        }
    )
    return records


@dataclass(frozen=True)
class GenerateResult:
    mcap_path: Path
    sidecar_path: Path
    sidecar: dict


def generate(
    out_dir,
    *,
    episodes: int = 3,
    duration: float = DEFAULT_DURATION_SECONDS,
    seed: int = DEFAULT_SEED,
    bad_episode: Optional[int] = None,
    robot_id: str = DEFAULT_ROBOT_ID,
    gap_seconds: float = DEFAULT_EPISODE_GAP_SECONDS,
    start_time_ns: int = DEFAULT_START_TIME_NS,
    image_width: int = DEFAULT_WIDTH,
    image_height: int = DEFAULT_HEIGHT,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    compression: str = "zstd",
    filename: Optional[str] = None,
) -> GenerateResult:
    """Write one MCAP + its sidecar; return both paths and the sidecar dict.

    `bad_episode` is 1-based (matching the CLI) and must be within
    1..`episodes`; None means every episode is clean.
    """
    if episodes < 1:
        raise ValueError("episodes must be >= 1")
    if duration <= 0:
        raise ValueError("duration must be > 0")
    if gap_seconds <= 10.0:
        raise ValueError(
            "gap_seconds must be > 10 so a 5 s gap-based segmenter splits episodes"
        )
    if bad_episode is not None and not (1 <= bad_episode <= episodes):
        raise ValueError(f"bad_episode must be in 1..{episodes}, got {bad_episode}")
    if compression not in COMPRESSION_CHOICES:
        raise ValueError(f"compression must be one of {sorted(COMPRESSION_CHOICES)}")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    name = filename or f"sim_{_sanitize(robot_id)}_seed{seed}.mcap"
    mcap_path = out_path / name
    sidecar_path = mcap_path.with_suffix(".sim.json")

    episode_records: List[dict] = []
    with open(mcap_path, "wb") as handle:
        writer = McapWriter(handle, COMPRESSION_CHOICES[compression])
        channels = {spec.topic: writer.channel_for(spec) for spec in TOPICS}
        cursor = 0.0
        for episode_index in range(1, episodes + 1):
            episode_records.append(
                _generate_episode(
                    writer,
                    channels,
                    episode_index=episode_index,
                    episode_start_ns=start_time_ns + int(round(cursor * 1e9)),
                    duration=duration,
                    seed=seed,
                    image_width=image_width,
                    image_height=image_height,
                    jpeg_quality=jpeg_quality,
                    is_bad=(bad_episode == episode_index),
                )
            )
            cursor += duration + gap_seconds
        writer.finish()

    digest = hashlib.sha256(mcap_path.read_bytes()).hexdigest()

    sidecar = {
        "sim_gen_version": SIM_GEN_VERSION,
        "sidecar_version": SIDECAR_VERSION,
        "mcap_file": mcap_path.name,
        "mcap_bytes": mcap_path.stat().st_size,
        "mcap_sha256": digest,
        "robot_id": robot_id,
        "seed": seed,
        "episode_count": episodes,
        "episode_duration_seconds": duration,
        "episode_gap_seconds": gap_seconds,
        "bad_episode": bad_episode,
        "start_time_ns": start_time_ns,
        "end_time_ns": episode_records[-1]["end_time_ns"],
        "message_encoding": "cdr",
        "schema_encoding": "ros2msg",
        "compression": compression,
        "image_width": image_width,
        "image_height": image_height,
        "jpeg_quality": jpeg_quality,
        "message_count": sum(r["message_count"] for r in episode_records),
        "topics": [
            {"topic": s.topic, "type": s.msgtype, "rate_hz": s.rate_hz}
            for s in TOPICS
        ],
        "regenerate_command": (
            "python -m sim_gen --out <DIR>"
            f" --episodes {episodes} --duration {duration:g} --seed {seed}"
            + (f" --bad-episode {bad_episode}" if bad_episode else "")
            + f" --robot-id {robot_id}"
        ),
        "episodes": episode_records,
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    return GenerateResult(mcap_path=mcap_path, sidecar_path=sidecar_path, sidecar=sidecar)
