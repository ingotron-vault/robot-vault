"""Tests for the simulation MCAP generator.

Honest labeling: these are DEV-TOOLING tests. They deliberately do NOT join the
unit gate in the Makefile -- sim_gen ships no runtime
code. Run them explicitly:

    docker compose run --rm dev pytest development/sim_gen/tests -q

The one thing these tests cannot prove is the thing that matters most -- that
the CDR bytes decode through the processor's schema-bound runtime path. That
is `development/sim_gen/verify_cdr.py`, run in the `robot-vault-processor`
image (see the module docstring there).
"""

from __future__ import annotations

import io
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest
from mcap.reader import make_reader
from PIL import Image

# Works whether or not /app/development is already on PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sim_gen import generate  # noqa: E402
from sim_gen.generator import (  # noqa: E402
    TASK_LABELS,
    TOPIC_CAMERA_INFO,
    TOPIC_CMD_VEL,
    TOPIC_IMAGE,
    TOPIC_IMU,
    TOPIC_ODOM,
    TOPIC_TASK,
    TOPIC_TF_STATIC,
    TOPICS,
    degradation_windows,
)
from sim_gen.messages import TYPESTORE  # noqa: E402

#: The segmenter gap threshold; episodes must be separated by
#: more than this and internally free of gaps this large.
SEGMENTER_GAP_SECONDS = 5.0

STD_DURATION = 15.0
STD_EPISODES = 3
STD_BAD_EPISODE = 2
STD_SEED = 42
# Small frames keep the suite fast; the pixel pipeline is identical.
STD_IMAGE = (64, 48)


def _read_messages(path: Path):
    """[(log_time_ns, topic, schema_name, decoded_msg, raw_bytes)] in log order."""
    rows = []
    with open(path, "rb") as handle:
        for schema, channel, message in make_reader(handle).iter_messages():
            assert channel.message_encoding == "cdr", channel.topic
            assert schema is not None and schema.encoding == "ros2msg", channel.topic
            rows.append(
                (
                    message.log_time,
                    channel.topic,
                    schema.name,
                    TYPESTORE.deserialize_cdr(message.data, schema.name),
                    message.data,
                )
            )
    rows.sort(key=lambda r: r[0])
    return rows


def _split_on_gap(rows, gap_seconds=SEGMENTER_GAP_SECONDS):
    """What a gap-based segmenter would produce: message silence > gap splits."""
    segments = []
    current = []
    previous = None
    for row in rows:
        if previous is not None and (row[0] - previous) > gap_seconds * 1e9:
            segments.append(current)
            current = []
        current.append(row)
        previous = row[0]
    if current:
        segments.append(current)
    return segments


def _sharpness(jpeg: bytes) -> float:
    """Variance of the Laplacian -- the standard blur metric; a Gaussian blur
    of radius 4 drops it by about two orders of magnitude."""
    arr = np.asarray(Image.open(io.BytesIO(jpeg)).convert("L"), dtype=np.float64)
    laplacian = (
        4.0 * arr[1:-1, 1:-1]
        - arr[:-2, 1:-1]
        - arr[2:, 1:-1]
        - arr[1:-1, :-2]
        - arr[1:-1, 2:]
    )
    return float(laplacian.var())


@pytest.fixture(scope="module")
def standard(tmp_path_factory):
    out = tmp_path_factory.mktemp("sim_std")
    result = generate(
        out,
        episodes=STD_EPISODES,
        duration=STD_DURATION,
        seed=STD_SEED,
        bad_episode=STD_BAD_EPISODE,
        image_width=STD_IMAGE[0],
        image_height=STD_IMAGE[1],
    )
    return result, _read_messages(result.mcap_path)


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_same_seed_produces_identical_bytes(tmp_path):
    kwargs = dict(
        episodes=2,
        duration=6.0,
        seed=11,
        bad_episode=2,
        image_width=32,
        image_height=24,
    )
    first = generate(tmp_path / "a", **kwargs)
    second = generate(tmp_path / "b", **kwargs)

    assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()
    assert first.sidecar_path.read_text() == second.sidecar_path.read_text()
    assert first.sidecar["mcap_sha256"] == second.sidecar["mcap_sha256"]


def test_different_seed_produces_different_bytes(tmp_path):
    kwargs = dict(episodes=1, duration=4.0, image_width=32, image_height=24)
    a = generate(tmp_path / "a", seed=1, **kwargs)
    b = generate(tmp_path / "b", seed=2, **kwargs)

    assert a.sidecar["mcap_sha256"] != b.sidecar["mcap_sha256"]
    # ... but the shape is unchanged: same topics, same message counts.
    assert a.sidecar["message_count"] == b.sidecar["message_count"]


def test_no_wall_clock_in_output(tmp_path):
    """Timestamps come from the fixed epoch, not time.time()."""
    result = generate(tmp_path, episodes=1, duration=2.0, seed=3, image_width=32, image_height=24)
    rows = _read_messages(result.mcap_path)
    assert rows[0][0] == result.sidecar["start_time_ns"]


# --------------------------------------------------------------------------
# episode / gap structure
# --------------------------------------------------------------------------


def test_episode_count_and_gap_structure(standard):
    result, rows = standard
    segments = _split_on_gap(rows)
    assert len(segments) == STD_EPISODES

    sidecar_episodes = result.sidecar["episodes"]
    assert [e["index"] for e in sidecar_episodes] == [1, 2, 3]

    for segment, episode in zip(segments, sidecar_episodes):
        assert len(segment) == episode["message_count"]
        assert segment[0][0] == episode["start_time_ns"]
        assert segment[-1][0] == episode["end_time_ns"]
        # No internal silence a 5 s segmenter would mistake for an episode
        # boundary -- not even the deliberate 5 s IMU dropout, because every
        # other topic keeps publishing through it.
        internal_gaps = np.diff([row[0] for row in segment]) / 1e9
        assert internal_gaps.max() < SEGMENTER_GAP_SECONDS

    # ... and the inter-episode silence is longer than 10 s, as promised.
    for previous, following in zip(segments, segments[1:]):
        assert (following[0][0] - previous[-1][0]) / 1e9 > 10.0


def test_episode_starts_with_task_label_and_static_tf(standard):
    result, rows = standard
    segments = _split_on_gap(rows)

    labels = []
    for segment, episode in zip(segments, result.sidecar["episodes"]):
        markers = [r for r in segment if r[1] == TOPIC_TASK]
        statics = [r for r in segment if r[1] == TOPIC_TF_STATIC]
        assert len(markers) == 1
        assert len(statics) == 1
        assert markers[0][0] == segment[0][0]
        assert markers[0][3].data == episode["task_label"]
        labels.append(episode["task_label"])

    assert all(label in TASK_LABELS for label in labels)
    assert len(set(labels)) == STD_EPISODES  # varies per episode


def test_all_declared_topics_and_rates_are_present(standard):
    result, rows = standard
    counts = defaultdict(int)
    for row in rows:
        counts[row[1]] += 1

    declared = {spec.topic: spec for spec in TOPICS}
    assert set(counts) == set(declared)
    for spec in TOPICS:
        if spec.rate_hz == 0.0:
            assert counts[spec.topic] == STD_EPISODES
        elif spec.topic == TOPIC_CAMERA_INFO:
            # absent for the bad episode by design
            assert counts[spec.topic] == int(spec.rate_hz * STD_DURATION) * (STD_EPISODES - 1)
        elif spec.topic == TOPIC_IMU:
            assert counts[spec.topic] < int(spec.rate_hz * STD_DURATION) * STD_EPISODES
        else:
            assert counts[spec.topic] == int(spec.rate_hz * STD_DURATION) * STD_EPISODES

    # Schema names must be exactly the ones the processor accepts, or every
    # message on the topic is silently skipped (processor/core.py allowlist).
    from robot_vault_config.topics import ROS2_TYPE_NORMALIZATION

    for row in rows:
        assert row[2] in ROS2_TYPE_NORMALIZATION, row[2]


def test_kinematics_are_self_consistent_in_good_episodes(standard):
    """odom twist and the wheel joint velocities must agree (differential drive)."""
    from sim_gen.motion import WHEEL_RADIUS_M, WHEEL_SEPARATION_M

    result, rows = standard
    good = _split_on_gap(rows)[0]
    odom = {r[0]: r[3] for r in good if r[1] == TOPIC_ODOM}
    joints = {r[0]: r[3] for r in good if r[1] == "/joint_states"}
    assert odom and set(odom) == set(joints)

    for stamp, message in odom.items():
        joint = joints[stamp]
        v = message.twist.twist.linear.x
        w = message.twist.twist.angular.z
        left, right = float(joint.velocity[0]), float(joint.velocity[1])
        assert math.isclose(left, (v - w * WHEEL_SEPARATION_M / 2) / WHEEL_RADIUS_M, rel_tol=1e-9, abs_tol=1e-9)
        assert math.isclose(right, (v + w * WHEEL_SEPARATION_M / 2) / WHEEL_RADIUS_M, rel_tol=1e-9, abs_tol=1e-9)


# --------------------------------------------------------------------------
# deliberately-bad episode: the sidecar must describe what is actually there
# --------------------------------------------------------------------------


def test_only_the_named_episode_is_degraded(standard):
    result, _rows = standard
    for episode in result.sidecar["episodes"]:
        if episode["index"] == STD_BAD_EPISODE:
            assert episode["degraded"] is True
            assert episode["degradations"]
        else:
            assert episode["degraded"] is False
            assert episode["degradations"] == []


def test_bad_episode_sidecar_lists_every_documented_defect(standard):
    result, _rows = standard
    bad = result.sidecar["episodes"][STD_BAD_EPISODE - 1]
    kinds = {d["kind"] for d in bad["degradations"]}
    assert kinds == {
        "imu_dropout",
        "camera_frozen",
        "camera_blur",
        "kinematic_implausibility",
        "missing_camera_info",
    }
    assert bad["degradations"] == [
        d for d in bad["degradations"] if d["topics"] and d["detail"]
    ]


def _bad_segment(result, rows):
    segment = _split_on_gap(rows)[STD_BAD_EPISODE - 1]
    episode = result.sidecar["episodes"][STD_BAD_EPISODE - 1]
    windows = {d["kind"]: d for d in episode["degradations"]}
    start = episode["start_time_ns"]
    return segment, windows, start


def _in(row_time_ns, start_ns, window):
    offset = (row_time_ns - start_ns) / 1e9
    return window["start_offset_s"] <= offset < window["end_offset_s"]


def test_bad_episode_has_no_camera_info(standard):
    result, rows = standard
    segments = _split_on_gap(rows)
    bad = segments[STD_BAD_EPISODE - 1]
    assert [r for r in bad if r[1] == TOPIC_CAMERA_INFO] == []
    for index, segment in enumerate(segments, start=1):
        if index != STD_BAD_EPISODE:
            assert [r for r in segment if r[1] == TOPIC_CAMERA_INFO]


def test_bad_episode_imu_dropout_matches_sidecar(standard):
    result, rows = standard
    segments = _split_on_gap(rows)
    segment, windows, _start = _bad_segment(result, rows)
    window = windows["imu_dropout"]
    expected = window["end_offset_s"] - window["start_offset_s"]

    stamps = [r[0] for r in segment if r[1] == TOPIC_IMU]
    largest = max(np.diff(stamps)) / 1e9
    assert expected == pytest.approx(5.0)
    assert largest == pytest.approx(expected, abs=0.05)

    for index, other in enumerate(segments, start=1):
        if index == STD_BAD_EPISODE:
            continue
        other_stamps = [r[0] for r in other if r[1] == TOPIC_IMU]
        assert max(np.diff(other_stamps)) / 1e9 < 0.05


def test_bad_episode_frozen_frames_are_byte_identical(standard):
    result, rows = standard
    segments = _split_on_gap(rows)
    segment, windows, start = _bad_segment(result, rows)
    window = windows["camera_frozen"]

    # The JPEG payload repeats; the header stamps keep advancing, which is
    # exactly what a stuck camera on a live bus looks like.
    frozen = [
        bytes(r[3].data)
        for r in segment
        if r[1] == TOPIC_IMAGE and _in(r[0], start, window)
    ]
    assert len(frozen) >= 10
    assert len(set(frozen)) == 1
    assert len({r[4] for r in segment if r[1] == TOPIC_IMAGE and _in(r[0], start, window)}) == len(frozen)

    for index, other in enumerate(segments, start=1):
        if index == STD_BAD_EPISODE:
            continue
        payloads = [bytes(r[3].data) for r in other if r[1] == TOPIC_IMAGE]
        assert len(set(payloads)) == len(payloads)


def test_bad_episode_blurred_frames_are_measurably_softer(standard):
    result, rows = standard
    segment, windows, start = _bad_segment(result, rows)
    blur = windows["camera_blur"]
    frozen = windows["camera_frozen"]

    inside, outside = [], []
    for row in segment:
        if row[1] != TOPIC_IMAGE:
            continue
        jpeg = bytes(row[3].data)
        if _in(row[0], start, blur):
            inside.append(_sharpness(jpeg))
        elif not _in(row[0], start, frozen):
            outside.append(_sharpness(jpeg))

    assert inside and outside
    assert np.median(inside) < 0.1 * np.median(outside)


def test_bad_episode_commands_motion_while_odom_is_stationary(standard):
    result, rows = standard
    segments = _split_on_gap(rows)
    segment, windows, start = _bad_segment(result, rows)
    window = windows["kinematic_implausibility"]

    commanded, reported = [], []
    for row in segment:
        if not _in(row[0], start, window):
            continue
        if row[1] == TOPIC_CMD_VEL:
            commanded.append(row[3].linear.x)
        elif row[1] == TOPIC_ODOM:
            reported.append(row[3].twist.twist.linear.x)

    assert commanded and reported
    assert min(commanded) > 0.01           # still being told to drive
    assert max(abs(v) for v in reported) == 0.0  # ... and going nowhere

    # Good episodes never look like that.
    for index, other in enumerate(segments, start=1):
        if index == STD_BAD_EPISODE:
            continue
        speeds = [r[3].twist.twist.linear.x for r in other if r[1] == TOPIC_ODOM]
        assert max(abs(v) for v in speeds) > 0.05


def test_degradation_windows_are_clipped_to_short_episodes():
    windows = degradation_windows(1.0)
    for name, window in windows.items():
        assert window is not None, name
        assert 0.0 <= window[0] < window[1] <= 1.0


# --------------------------------------------------------------------------
# argument validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"episodes": 0},
        {"duration": 0.0},
        {"gap_seconds": 10.0},  # would not survive a 10 s silence requirement
        {"bad_episode": 4},
        {"compression": "brotli"},
    ],
)
def test_invalid_arguments_are_rejected(tmp_path, override):
    kwargs = {"episodes": 3, "duration": 1.0, "seed": 0}
    kwargs.update(override)
    with pytest.raises(ValueError):
        generate(tmp_path, **kwargs)


def test_sidecar_round_trips_as_json(tmp_path):
    result = generate(tmp_path, episodes=1, duration=2.0, seed=5, image_width=32, image_height=24)
    on_disk = json.loads(result.sidecar_path.read_text())
    assert on_disk["mcap_file"] == result.mcap_path.name
    assert on_disk["mcap_sha256"] == result.sidecar["mcap_sha256"]
    assert on_disk["message_encoding"] == "cdr"
    assert on_disk["schema_encoding"] == "ros2msg"
    assert "python -m sim_gen" in on_disk["regenerate_command"]
