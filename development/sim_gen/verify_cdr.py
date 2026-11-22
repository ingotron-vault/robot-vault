#!/usr/bin/env python3
r"""Acceptance check for a sim_gen MCAP: decode it with the shipped CDR path.

Runs inside the `robot-vault-processor` image, NOT in the dev image. The
processor's official mcap-ros2-support DecoderFactory reads each MCAP's
embedded ros2msg schema with no ROS apt runtime. This check proves the bytes
sim_gen writes with rosbags decode through that same schema-bound path; a JSON
fallback or a guessed generated message class would not prove that.

    docker compose run --rm --no-deps \
        -v $PWD/tmp_simgen:/fixtures -v $PWD/development:/development \
        processor python3 /development/sim_gen/verify_cdr.py /fixtures/<file>.mcap

Deliberately stdlib-only plus `mcap`, `mcap-ros2-support` and `processor` --
rosbags, Pillow and numpy are dev-image dependencies and are not installed
here.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Dict, Optional

from mcap.reader import make_reader

from processor.cdr_decode import McapRos2Decoder, get_attr, try_decode_cdr
from processor.extractors import extract_xyz
from processor.supported_types import ROS2_TYPE_NORMALIZATION

#: Fields that must come back non-null once the message is decoded. Proves we
#: are reading real CDR structure, not just "deserialize_message did not raise".
REQUIRED_FIELDS: Dict[str, tuple] = {
    "sensor_msgs/msg/Imu": (
        "header.stamp.sec",
        "linear_acceleration.z",
        "angular_velocity.z",
        "orientation.w",
    ),
    "geometry_msgs/msg/Twist": ("linear.x", "angular.z"),
    "nav_msgs/msg/Odometry": (
        "header.frame_id",
        "child_frame_id",
        "pose.pose.position.x",
        "pose.pose.orientation.w",
        "twist.twist.linear.x",
    ),
    "sensor_msgs/msg/CompressedImage": ("header.frame_id", "format", "data"),
    "sensor_msgs/msg/CameraInfo": ("width", "height", "distortion_model", "k"),
    "sensor_msgs/msg/JointState": ("name", "position", "velocity"),
    "tf2_msgs/msg/TFMessage": ("transforms",),
    "sensor_msgs/msg/NavSatFix": ("latitude", "longitude", "altitude"),
    "std_msgs/msg/String": ("data",),
}

#: Schemas whose decoded values must also survive the processor's real
#: extractor (the function that fills x/y/z in Parquet).
EXTRACTOR_SCHEMAS = (
    "sensor_msgs/msg/Imu",
    "geometry_msgs/msg/Twist",
    "nav_msgs/msg/Odometry",
    "sensor_msgs/msg/NavSatFix",
)


def _fail(errors: list, message: str) -> None:
    if len(errors) < 20:
        errors.append(message)


def verify(path: str, sidecar_path: Optional[str] = None) -> int:
    errors: list = []
    decoded: Dict[str, int] = defaultdict(int)
    failed: Dict[str, int] = defaultdict(int)
    extracted: Dict[str, int] = defaultdict(int)
    topics: Dict[str, str] = {}
    log_times: list = []
    task_labels: list = []
    decoder = McapRos2Decoder()

    with open(path, "rb") as handle:
        reader = make_reader(handle)
        for schema, channel, message in reader.iter_messages():
            schema_name = schema.name if schema else ""
            topics.setdefault(channel.topic, schema_name)
            log_times.append(message.log_time)

            if channel.message_encoding != "cdr":
                _fail(
                    errors,
                    f"{channel.topic}: message_encoding is "
                    f"{channel.message_encoding!r}, expected 'cdr'",
                )
                continue
            if schema is not None and schema.encoding != "ros2msg":
                _fail(
                    errors,
                    f"{channel.topic}: schema encoding is {schema.encoding!r}, "
                    "expected 'ros2msg'",
                )
            if schema_name not in ROS2_TYPE_NORMALIZATION:
                _fail(
                    errors,
                    f"{channel.topic}: schema {schema_name!r} is not in the "
                    "processor's supported-type allowlist -- the processor "
                    "would skip every message on this topic",
                )

            msg = try_decode_cdr(
                schema_name,
                message.data,
                decoder=decoder,
                schema=schema,
                message_encoding=channel.message_encoding,
            )
            if msg is None:
                failed[schema_name] += 1
                _fail(errors, f"{channel.topic}: CDR decode returned None ({schema_name})")
                continue
            decoded[schema_name] += 1

            for field in REQUIRED_FIELDS.get(schema_name, ()):
                value = get_attr(msg, field)
                if value is None or (isinstance(value, str) and field == "format" and not value):
                    _fail(
                        errors,
                        f"{channel.topic}: decoded {schema_name} has null {field}",
                    )
            if schema_name == "sensor_msgs/msg/CompressedImage":
                blob = bytes(get_attr(msg, "data") or b"")
                if not blob.startswith(b"\xff\xd8\xff"):
                    _fail(errors, f"{channel.topic}: payload is not a JPEG")
            if schema_name == "std_msgs/msg/String":
                task_labels.append(get_attr(msg, "data"))

            if schema_name in EXTRACTOR_SCHEMAS:
                x, y, z = extract_xyz(
                    schema_name,
                    channel.topic,
                    channel.message_encoding,
                    message.data,
                    cdr_decoder=decoder,
                    schema=schema,
                )
                if x is None or y is None or z is None:
                    _fail(
                        errors,
                        f"{channel.topic}: processor.extractors.extract_xyz "
                        f"returned ({x}, {y}, {z}) for {schema_name}",
                    )
                else:
                    extracted[schema_name] += 1

    if not log_times:
        _fail(errors, "no messages in file")

    print(f"file: {path}")
    print(f"messages: {len(log_times)}  topics: {len(topics)}")
    for topic in sorted(topics):
        print(f"  {topic:32s} {topics[topic]}")
    print("decoded per schema (embedded ros2msg/CDR):")
    for schema_name in sorted(decoded):
        note = f", extract_xyz ok on {extracted[schema_name]}" if schema_name in extracted else ""
        print(f"  {schema_name:40s} {decoded[schema_name]} ok, {failed.get(schema_name, 0)} failed{note}")
    print(f"task labels seen: {sorted(set(task_labels))}")

    if sidecar_path:
        sidecar = json.loads(open(sidecar_path).read())
        expected = sidecar["message_count"]
        if expected != len(log_times):
            _fail(
                errors,
                f"sidecar message_count {expected} != {len(log_times)} read back",
            )
        else:
            print(f"sidecar message_count matches: {expected}")

    if errors:
        print(f"\nFAIL: {len(errors)} problem(s)", file=sys.stderr)
        for line in errors:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print("\nPASS: every message decoded through the processor's embedded-schema CDR path")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mcap")
    parser.add_argument("--sidecar", default=None)
    args = parser.parse_args()
    return verify(args.mcap, args.sidecar)


if __name__ == "__main__":
    sys.exit(main())
