from __future__ import annotations

import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from example_data.generator import (
    ALOHA_JOINT_NAMES,
    CameraCalibration,
    TimedImage,
    TimedOdom,
    TimedScan,
    TimedTwist,
    TumClip,
    _inventory,
    _derive_pose_twist,
    _write_aloha_bag,
    _write_pii_bag,
    _write_tum_bag,
)


def jpeg(colour=(40, 80, 120)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (96, 72), colour).save(out, "JPEG")
    return out.getvalue()


def tum_clip() -> TumClip:
    images = tuple(TimedImage(i * 200_000_000, jpeg((40 + i, 80, 120))) for i in range(40))
    twists = tuple(
        TimedTwist(i * 100_000_000, 0.2, 0.0, 0.0, 0.0, 0.0, 0.3)
        for i in range(80)
    )
    odom = tuple(
        TimedOdom(i * 50_000_000, i / 100.0, 0.0, i / 200.0, 0.2, 0.3)
        for i in range(160)
    )
    scan = TimedScan(
        0,
        -1.0,
        1.0,
        0.5,
        0.0,
        0.2,
        0.1,
        10.0,
        (1.0, 1.1, 1.2, 1.3, 1.4),
        (),
    )
    return TumClip(
        source_start_ns=1,
        duration_ns=8_000_000_000,
        images=images,
        twists=twists,
        odometry=odom,
        scans=tuple(
            TimedScan(
                i * 200_000_000,
                scan.angle_min,
                scan.angle_max,
                scan.angle_increment,
                scan.time_increment,
                scan.scan_time,
                scan.range_min,
                scan.range_max,
                scan.ranges,
                scan.intensities,
            )
            for i in range(40)
        ),
        calibration=CameraCalibration(
            480,
            360,
            (400.0, 0.0, 240.0, 0.0, 400.0, 180.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 0.0, 0.0, 0.0),
            "plumb_bob",
        ),
        source_topics={"camera": "/camera", "twist": "/cmd_vel", "odometry": "/odom", "laser": "/scan"},
    )


def test_tum_variants_are_real_decodable_mcaps_and_faults_live_in_bytes(tmp_path):
    clean = tmp_path / "clean.mcap"
    bad = tmp_path / "bad.mcap"
    clean_meta = _write_tum_bag(clean, tum_clip(), imperfect=False)
    bad_meta = _write_tum_bag(bad, tum_clip(), imperfect=True)

    clean_inventory = _inventory(clean)
    bad_inventory = _inventory(bad)
    assert clean_inventory["message_count"] == clean_meta["message_count"]
    assert bad_inventory["message_count"] == bad_meta["message_count"]
    assert "/camera/rgb/camera_info" in clean_inventory["topic_counts"]
    assert "/camera/rgb/camera_info" not in bad_inventory["topic_counts"]
    assert "/tf_static" not in bad_inventory["topic_counts"]
    assert bad_meta["injections"]["camera_frames_dropped"] > 0
    assert bad_meta["injections"]["dynamic_tf_messages_dropped"] > 0


def test_pose_derivative_is_labeled_measured_twist_with_real_angular_z():
    source = tuple(
        TimedOdom(i * 50_000_000, i * 0.01, 0.0, i * 0.01, 0.0, 0.0)
        for i in range(8)
    )
    odometry, twists = _derive_pose_twist(source)

    assert len(odometry) == len(source)
    assert len(twists) == 4
    assert all(sample.linear_x > 0 for sample in twists)
    assert all(sample.angular_z > 0 for sample in twists)


def test_aloha_adapter_preserves_all_joint_dimensions_and_adds_named_projection(tmp_path):
    rows = 401
    timestamps = [i * 0.02 for i in range(rows)]
    states = [[0.01 * j + 0.0001 * i for j in range(14)] for i in range(rows)]
    actions = [[0.02 * j + 0.0002 * i * (j + 1) for j in range(14)] for i in range(rows)]
    table = pa.table(
        {
            "observation.state": states,
            "observation.effort": [[float(j) for j in range(14)] for _ in range(rows)],
            "action": actions,
            "episode_index": [0] * rows,
            "frame_index": list(range(rows)),
            "timestamp": timestamps,
        }
    )
    source = tmp_path / "aloha.parquet"
    pq.write_table(table, source)
    output = tmp_path / "aloha.mcap"
    metadata = _write_aloha_bag(output, source)
    inventory = _inventory(output)

    assert metadata["joint_dimensions"] == len(ALOHA_JOINT_NAMES) == 14
    assert metadata["source_rows"] >= 100
    assert metadata["max_abs_projected_angular_z"] > 0
    assert inventory["schemas"]["/action/joint_targets"] == "sensor_msgs/msg/JointState"
    assert inventory["schemas"]["/action/twist_projection"] == "geometry_msgs/msg/Twist"


def test_pii_fixture_is_fully_synthetic_and_reuses_acceptance_generator(tmp_path):
    face = tmp_path / "face.png"
    Image.new("RGB", (256, 256), (180, 130, 100)).save(face)
    output = tmp_path / "pii.mcap"
    metadata = _write_pii_bag(output, face)
    inventory = _inventory(output)

    assert metadata["synthetic_camera_frames"] == 80
    assert metadata["synthetic_plate_text"] == "SYN 2026"
    assert "No real person" in metadata["privacy_claim"]
    assert inventory["topic_counts"]["/camera/image_compressed"] == 80
