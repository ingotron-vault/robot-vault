"""Typed, per-message-type extraction for the v2 schema families.

`extractors.extract_xyz` answers one question for every message type in the
catalog: "what are the three numbers to plot?". That is the v1 schema, and
it stays exactly as it is. This module answers a different question for the
six common robot message types -- "what does this
message SAY?" -- and produces one row dict per record inside the message:

    sensor_msgs/msg/JointState  -> joint_state       (one row per joint)
    nav_msgs/msg/Odometry       -> odometry          (one row per message)
    geometry_msgs/msg/Twist     -> twist             (one row per message)
    tf2_msgs/msg/TFMessage      -> tf                (one row per transform)
    sensor_msgs/msg/CameraInfo  -> camera_info       (one row per message)
    sensor_msgs/msg/PointCloud2 -> pointcloud_stats  (one row per message)

Each family name maps to a fixed Arrow schema in `parquet_schema.py`, and
the row dicts here carry exactly that schema's payload columns -- `ts` and
`topic` are added by the writer in `core.py`, which owns them for v1 too.
A key a schema does not have is a bug; a key it has but a message does not
populate is simply absent from the dict and lands as NULL.

Both decode paths, mirroring `extractors.py`:
  - CDR (a real ROS 2 bag) via `cdr_decode.try_decode_cdr` + `get_attr`,
    bound to the MCAP's embedded ros2msg schema by a per-file dynamic decoder;
  - JSON payloads (`message_encoding == "json"`) for dev/test fixtures and
    the simulation generator, accepting the field spellings rosbridge-style
    encoders actually emit.

Decode accounting is deliberately NOT shared with `extract_xyz`: this
module never touches the caller's `decode_stats`, so the per-upload
"~100% of CDR decodes failed" gate (`DecodeFailureError`) keeps counting
exactly the attempts of the v1 path. The cost is one extra
dynamic CDR deserialization per message for these six types; the alternative --
threading a pre-decoded message through `extract_xyz` -- would change the
v1 extraction path, which must stay unchanged.

`twist` follows the same rules; see the phase 04 technical guide's
"v2 typed schema families" section. The v1 `(x, y, z) = linear.{x,y,z}` rows
a Twist message produces are still written, and the typed row is an
additional file that carries the angular half. Uploads processed before
`twist` was added have no `twist` family and are not rewritten.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .cdr_decode import McapRos2Decoder, get_attr, try_decode_cdr

# Schema name -> family name. The keys are the exact `schema.name` values
# MCAP carries for these types (and the exact keys in the shared catalog,
# `robot_vault_config.topics.ROS2_TYPE_NORMALIZATION`), so a message either
# has a typed family or it does not -- there is no fuzzy matching here.
TYPED_FAMILY_BY_SCHEMA: Dict[str, str] = {
    "sensor_msgs/msg/JointState": "joint_state",
    "nav_msgs/msg/Odometry": "odometry",
    "geometry_msgs/msg/Twist": "twist",
    "tf2_msgs/msg/TFMessage": "tf",
    "sensor_msgs/msg/CameraInfo": "camera_info",
    "sensor_msgs/msg/PointCloud2": "pointcloud_stats",
}

#: Upper bound on points read out of ONE PointCloud2 message for its
#: bounds. A 128-beam lidar sweep is ~250k points; parsing all of them per
#: message in Python would dominate processing time for a stats-only
#: product. Sample positions are evenly distributed (not the first N points),
#: so the estimate spans the whole sweep rather than one corner of it.
MAX_POINTCLOUD_SAMPLE = 1024

# sensor_msgs/msg/PointField datatype enum -> (struct format, byte size).
_POINTFIELD_FORMATS: Dict[int, Tuple[str, int]] = {
    1: ("b", 1),   # INT8
    2: ("B", 1),   # UINT8
    3: ("h", 2),   # INT16
    4: ("H", 2),   # UINT16
    5: ("i", 4),   # INT32
    6: ("I", 4),   # UINT32
    7: ("f", 4),   # FLOAT32
    8: ("d", 8),   # FLOAT64
}

_CDR_ENCODINGS = ("cdr", "cdr_endian", "cdr_endianness", "ros2")


def family_for_schema(schema_name: Optional[str]) -> Optional[str]:
    """Typed family for a ROS 2 schema name, or None if it has no typed
    extractor (which is most of the catalog)."""
    return TYPED_FAMILY_BY_SCHEMA.get(schema_name or "")


def extract_typed_rows(
    schema_name: str,
    topic: str,
    message_encoding: Optional[str],
    data: Optional[bytes],
    *,
    cdr_decoder: Optional[McapRos2Decoder] = None,
    schema: Optional[object] = None,
) -> List[Dict[str, Any]]:
    """Payload rows for one message, or [] if nothing could be extracted.

    Returns a list because a single message can carry many records (a
    JointState of N joints, a TFMessage of N transforms). `topic` is the
    RAW topic name -- `tf` reads `/tf_static` off it -- while the `topic`
    COLUMN written to Parquet is the normalized bucket, added by the
    caller exactly as it is for v1.
    """
    family = family_for_schema(schema_name)
    if family is None:
        return []

    enc = (message_encoding or "").lower()
    if enc in _CDR_ENCODINGS:
        msg = try_decode_cdr(
            schema_name, data, decoder=cdr_decoder, schema=schema,
            message_encoding=message_encoding,
        )
        if msg is not None:
            return _rows_from_ros2(family, topic, msg)
        # Fall through: a CDR decode failure is already counted (per schema)
        # by `try_decode_cdr` and, for the upload-level gate, by
        # `extract_xyz`'s own attempt on this same message.
        return []

    if enc == "json":
        obj = _json_try_parse(data)
        if isinstance(obj, dict):
            return _rows_from_json(family, topic, obj)
    return []


# --- CDR path ---------------------------------------------------------------

def _rows_from_ros2(family: str, topic: str, msg: object) -> List[Dict[str, Any]]:
    if family == "joint_state":
        return _joint_state_rows(
            names=_seq(get_attr(msg, "name")),
            position=_seq(get_attr(msg, "position")),
            velocity=_seq(get_attr(msg, "velocity")),
            effort=_seq(get_attr(msg, "effort")),
        )

    if family == "odometry":
        return [
            _odometry_row(
                position=(
                    get_attr(msg, "pose.pose.position.x"),
                    get_attr(msg, "pose.pose.position.y"),
                    get_attr(msg, "pose.pose.position.z"),
                ),
                orientation=(
                    get_attr(msg, "pose.pose.orientation.x"),
                    get_attr(msg, "pose.pose.orientation.y"),
                    get_attr(msg, "pose.pose.orientation.z"),
                    get_attr(msg, "pose.pose.orientation.w"),
                ),
                linear=(
                    get_attr(msg, "twist.twist.linear.x"),
                    get_attr(msg, "twist.twist.linear.y"),
                    get_attr(msg, "twist.twist.linear.z"),
                ),
                angular=(
                    get_attr(msg, "twist.twist.angular.x"),
                    get_attr(msg, "twist.twist.angular.y"),
                    get_attr(msg, "twist.twist.angular.z"),
                ),
            )
        ]

    if family == "twist":
        return [
            _twist_row(
                linear=(
                    get_attr(msg, "linear.x"),
                    get_attr(msg, "linear.y"),
                    get_attr(msg, "linear.z"),
                ),
                angular=(
                    get_attr(msg, "angular.x"),
                    get_attr(msg, "angular.y"),
                    get_attr(msg, "angular.z"),
                ),
            )
        ]

    if family == "tf":
        is_static = is_static_tf_topic(topic)
        rows = []
        for tfm in _seq(get_attr(msg, "transforms")):
            rows.append(
                _tf_row(
                    parent=get_attr(tfm, "header.frame_id"),
                    child=get_attr(tfm, "child_frame_id"),
                    translation=(
                        get_attr(tfm, "transform.translation.x"),
                        get_attr(tfm, "transform.translation.y"),
                        get_attr(tfm, "transform.translation.z"),
                    ),
                    rotation=(
                        get_attr(tfm, "transform.rotation.x"),
                        get_attr(tfm, "transform.rotation.y"),
                        get_attr(tfm, "transform.rotation.z"),
                        get_attr(tfm, "transform.rotation.w"),
                    ),
                    is_static=is_static,
                )
            )
        return rows

    if family == "camera_info":
        # `k` is the 3x3 intrinsic matrix, row-major: [fx 0 cx; 0 fy cy; 0 0 1].
        # ROS message definitions name it `k`; some encoders keep ROS 1's `K`.
        k = _seq(get_attr(msg, "k")) or _seq(get_attr(msg, "K"))
        return [
            _camera_info_row(
                width=get_attr(msg, "width"),
                height=get_attr(msg, "height"),
                k=k,
                distortion_model=get_attr(msg, "distortion_model"),
            )
        ]

    if family == "pointcloud_stats":
        fields = [
            (
                get_attr(f, "name"),
                get_attr(f, "offset"),
                get_attr(f, "datatype"),
            )
            for f in _seq(get_attr(msg, "fields"))
        ]
        return [
            _pointcloud_stats_row(
                fields=fields,
                point_step=get_attr(msg, "point_step"),
                row_step=get_attr(msg, "row_step"),
                width=get_attr(msg, "width"),
                height=get_attr(msg, "height"),
                data=_as_bytes(get_attr(msg, "data")),
                is_bigendian=bool(get_attr(msg, "is_bigendian")),
            )
        ]

    return []


# --- JSON path --------------------------------------------------------------

def _rows_from_json(family: str, topic: str, obj: dict) -> List[Dict[str, Any]]:
    if family == "joint_state":
        return _joint_state_rows(
            names=_seq(obj.get("name")),
            position=_seq(obj.get("position")),
            velocity=_seq(obj.get("velocity")),
            effort=_seq(obj.get("effort")),
        )

    if family == "odometry":
        pose = _dig(obj, "pose", "pose") or {}
        twist = _dig(obj, "twist", "twist") or {}
        position = pose.get("position") or {}
        orientation = pose.get("orientation") or {}
        linear = twist.get("linear") or {}
        angular = twist.get("angular") or {}
        return [
            _odometry_row(
                position=(position.get("x"), position.get("y"), position.get("z")),
                orientation=(
                    orientation.get("x"), orientation.get("y"),
                    orientation.get("z"), orientation.get("w"),
                ),
                linear=(linear.get("x"), linear.get("y"), linear.get("z")),
                angular=(angular.get("x"), angular.get("y"), angular.get("z")),
            )
        ]

    if family == "twist":
        linear = _dig(obj, "linear") or {}
        angular = _dig(obj, "angular") or {}
        return [
            _twist_row(
                linear=(linear.get("x"), linear.get("y"), linear.get("z")),
                angular=(angular.get("x"), angular.get("y"), angular.get("z")),
            )
        ]

    if family == "tf":
        is_static = is_static_tf_topic(topic)
        rows = []
        for tfm in _seq(obj.get("transforms")):
            if not isinstance(tfm, dict):
                continue
            transform = tfm.get("transform") or {}
            translation = transform.get("translation") or {}
            rotation = transform.get("rotation") or {}
            rows.append(
                _tf_row(
                    parent=(tfm.get("header") or {}).get("frame_id"),
                    child=tfm.get("child_frame_id"),
                    translation=(
                        translation.get("x"), translation.get("y"), translation.get("z"),
                    ),
                    rotation=(
                        rotation.get("x"), rotation.get("y"),
                        rotation.get("z"), rotation.get("w"),
                    ),
                    is_static=is_static,
                )
            )
        return rows

    if family == "camera_info":
        return [
            _camera_info_row(
                width=obj.get("width"),
                height=obj.get("height"),
                k=_seq(obj.get("k")) or _seq(obj.get("K")),
                distortion_model=obj.get("distortion_model"),
            )
        ]

    if family == "pointcloud_stats":
        fields = [
            (f.get("name"), f.get("offset"), f.get("datatype"))
            for f in _seq(obj.get("fields"))
            if isinstance(f, dict)
        ]
        return [
            _pointcloud_stats_row(
                fields=fields,
                point_step=obj.get("point_step"),
                row_step=obj.get("row_step"),
                width=obj.get("width"),
                height=obj.get("height"),
                data=_as_bytes(obj.get("data")),
                is_bigendian=bool(obj.get("is_bigendian")),
            )
        ]

    return []


# --- row builders (shared by both decode paths) -----------------------------

def _joint_state_rows(
    *,
    names: Sequence[Any],
    position: Sequence[Any],
    velocity: Sequence[Any],
    effort: Sequence[Any],
) -> List[Dict[str, Any]]:
    """One row per joint.

    The four arrays are parallel but independently optional in the ROS
    definition: a controller may publish positions only, and `velocity`/
    `effort` are then empty -- which is a NULL column for that joint, not a
    reason to drop the row. The row count follows the LONGEST array so a
    message whose `name` array is short (malformed, but real: `name` is not
    required by the message definition) still yields its numbers, under a
    positional `joint_{i}` label rather than being silently discarded.
    """
    n = max(len(names), len(position), len(velocity), len(effort))
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        raw_name = names[i] if i < len(names) else None
        rows.append(
            {
                "joint_name": str(raw_name) if raw_name is not None else f"joint_{i}",
                "position": _num(position[i]) if i < len(position) else None,
                "velocity": _num(velocity[i]) if i < len(velocity) else None,
                "effort": _num(effort[i]) if i < len(effort) else None,
            }
        )
    return rows


def _odometry_row(
    *,
    position: Tuple[Any, Any, Any],
    orientation: Tuple[Any, Any, Any, Any],
    linear: Tuple[Any, Any, Any],
    angular: Tuple[Any, Any, Any],
) -> Dict[str, Any]:
    return {
        "px": _num(position[0]), "py": _num(position[1]), "pz": _num(position[2]),
        "qx": _num(orientation[0]), "qy": _num(orientation[1]),
        "qz": _num(orientation[2]), "qw": _num(orientation[3]),
        "vx": _num(linear[0]), "vy": _num(linear[1]), "vz": _num(linear[2]),
        "wx": _num(angular[0]), "wy": _num(angular[1]), "wz": _num(angular[2]),
    }


def _twist_row(
    *,
    linear: Tuple[Any, Any, Any],
    angular: Tuple[Any, Any, Any],
) -> Dict[str, Any]:
    """One commanded velocity, all six components.

    A component the message did not carry stays NULL rather than becoming a
    zero: on a velocity command those two are not the same statement -- zero
    is "hold still", NULL is "this recording never said". Downstream
    consumers must be able to tell the two apart.
    """
    return {
        "linear_x": _num(linear[0]),
        "linear_y": _num(linear[1]),
        "linear_z": _num(linear[2]),
        "angular_x": _num(angular[0]),
        "angular_y": _num(angular[1]),
        "angular_z": _num(angular[2]),
    }


def _tf_row(
    *,
    parent: Any,
    child: Any,
    translation: Tuple[Any, Any, Any],
    rotation: Tuple[Any, Any, Any, Any],
    is_static: bool,
) -> Dict[str, Any]:
    return {
        "parent_frame": str(parent) if parent is not None else None,
        "child_frame": str(child) if child is not None else None,
        "tx": _num(translation[0]), "ty": _num(translation[1]), "tz": _num(translation[2]),
        "qx": _num(rotation[0]), "qy": _num(rotation[1]),
        "qz": _num(rotation[2]), "qw": _num(rotation[3]),
        "is_static": bool(is_static),
    }


def _camera_info_row(
    *, width: Any, height: Any, k: Sequence[Any], distortion_model: Any
) -> Dict[str, Any]:
    def _k(index: int) -> Optional[float]:
        return _num(k[index]) if len(k) > index else None

    return {
        "width": _int(width),
        "height": _int(height),
        "fx": _k(0),
        "cx": _k(2),
        "fy": _k(4),
        "cy": _k(5),
        "distortion_model": str(distortion_model) if distortion_model else None,
    }


def _pointcloud_stats_row(
    *,
    fields: Sequence[Tuple[Any, Any, Any]],
    point_step: Any,
    row_step: Any,
    width: Any,
    height: Any,
    data: Optional[bytes],
    is_bigendian: bool,
) -> Dict[str, Any]:
    """Bounds over a bounded, evenly distributed sample of the cloud.

    Never decodes the whole cloud: it reads the x/y/z field offsets out of
    the message's own `fields`/`point_step`/`row_step` layout and unpacks at
    most `MAX_POINTCLOUD_SAMPLE` logical points, spread evenly across the
    cloud. NaN and infinite coordinates are skipped -- lidar drivers use them
    as "no return" markers, and one of them would otherwise poison every bound
    in the row.
    """
    width_i = max(0, _int(width) or 0)
    height_i = max(0, _int(height) or 0)
    step = _int(point_step) or 0

    # `n_points` is what the message claims (width x height). What can
    # actually be read is bounded by the buffer and the declared row layout.
    # A truncated payload therefore shows sample_n < n_points rather than
    # silently rewriting the message's own count.
    declared = width_i * height_i
    contiguous_points = (len(data) // step) if (data and step > 0) else 0
    n_points = declared if declared > 0 else contiguous_points

    row: Dict[str, Any] = {
        "n_points": int(n_points),
        "sample_n": 0,
        "x_min": None, "x_max": None,
        "y_min": None, "y_max": None,
        "z_min": None, "z_max": None,
    }

    offsets = _xyz_field_layout(fields)
    if offsets is None or step <= 0 or not data:
        return row

    # A field is part of one point and may not reach into the next point (or
    # into row padding). Without this check a malformed offset can produce
    # plausible-looking coordinates read from neighbouring bytes.
    if any(offset < 0 or offset + size > step for offset, _fmt, size in offsets):
        return row

    logical_points = n_points
    if logical_points <= 0:
        return row

    # Organized PointCloud2 rows may have padding (`row_step` can exceed
    # width * point_step). Logical point N therefore does not necessarily
    # start at N * point_step. A missing row_step is common in JSON fixtures,
    # so absence falls back to tightly packed; an explicit zero/short stride
    # is an invalid ROS layout and is not guessed through.
    organized = declared > 0
    raw_row_stride = _int(row_step)
    row_stride = (
        width_i * step
        if organized and raw_row_stride is None
        else (raw_row_stride or 0)
    )
    if organized and (row_stride <= 0 or row_stride < width_i * step):
        return row

    endian = ">" if is_bigendian else "<"

    mins: List[Optional[float]] = [None, None, None]
    maxs: List[Optional[float]] = [None, None, None]
    sample_n = 0
    for index in _evenly_spaced_indices(logical_points, MAX_POINTCLOUD_SAMPLE):
        if organized:
            row_index, column_index = divmod(index, width_i)
            base = row_index * row_stride + column_index * step
        else:
            base = index * step
        values: List[float] = []
        for offset, fmt, size in offsets:
            start = base + offset
            if start + size > len(data):
                values = []
                break
            try:
                (value,) = struct.unpack_from(endian + fmt, data, start)
            except struct.error:
                values = []
                break
            values.append(float(value))
        if len(values) != 3 or not all(math.isfinite(v) for v in values):
            continue
        sample_n += 1
        for axis, value in enumerate(values):
            if mins[axis] is None or value < mins[axis]:
                mins[axis] = value
            if maxs[axis] is None or value > maxs[axis]:
                maxs[axis] = value

    row["sample_n"] = sample_n
    if sample_n:
        row["x_min"], row["y_min"], row["z_min"] = mins
        row["x_max"], row["y_max"], row["z_max"] = maxs
    return row


def _evenly_spaced_indices(count: int, limit: int) -> List[int]:
    """At most ``limit`` logical positions spanning ``[0, count - 1]``.

    A ``range(0, count, count // limit)[:limit]`` sampler has a systematic
    front bias: for 5,000 points and a 1,024-point budget it never inspects
    the final 907 points. Integer interpolation includes both ends
    and makes adjacent gaps differ by at most one, while staying deterministic
    and allocation-bounded.
    """
    count_i = max(0, int(count))
    limit_i = max(0, int(limit))
    if count_i == 0 or limit_i == 0:
        return []
    if count_i <= limit_i:
        return list(range(count_i))
    if limit_i == 1:
        return [(count_i - 1) // 2]

    intervals = limit_i - 1
    last = count_i - 1
    # Add half an interval before integer division for round-to-nearest rather
    # than floor bias. Because count > limit, the results remain unique.
    return [
        (sample * last + intervals // 2) // intervals
        for sample in range(limit_i)
    ]


def _xyz_field_layout(
    fields: Sequence[Tuple[Any, Any, Any]],
) -> Optional[List[Tuple[int, str, int]]]:
    """(offset, struct format, size) for the x/y/z fields, or None.

    None means "this cloud does not carry x/y/z in a layout we can read"
    (no such fields, or an unknown datatype enum) -- the caller then
    records `n_points` with NULL bounds rather than guessing.
    """
    by_name: Dict[str, Tuple[int, str, int]] = {}
    for name, offset, datatype in fields:
        key = str(name or "").lower()
        if key not in ("x", "y", "z"):
            continue
        spec = _POINTFIELD_FORMATS.get(_int(datatype) or 0)
        offset_i = _int(offset)
        if spec is None or offset_i is None:
            continue
        by_name[key] = (offset_i, spec[0], spec[1])
    if len(by_name) != 3:
        return None
    return [by_name["x"], by_name["y"], by_name["z"]]


def is_static_tf_topic(topic: Optional[str]) -> bool:
    """Whether a raw topic name is the static-transform topic.

    `/tf` and `/tf_static` are the same message type and normalize to the
    same `tf` bucket, so the distinction has to be read off the raw topic
    -- and it matters: static TF is the robot's calibration, which downstream QA
    can check for completeness.
    """
    name = (topic or "").strip().lower().rstrip("/")
    return name.rsplit("/", 1)[-1] == "tf_static"


# --- small helpers ----------------------------------------------------------

def _json_try_parse(data: Optional[bytes]) -> Optional[object]:
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _dig(obj: dict, *path: str) -> Optional[dict]:
    """Nested dict lookup that tolerates a missing or wrongly-typed level."""
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else None


def _seq(value: Any) -> List[Any]:
    """A message's repeated field as a plain list.

    ROS 2 hands these back as `array.array` or numpy arrays depending on
    the field type, JSON hands back lists, and an absent field is None --
    all of which have to iterate the same way here. A str/bytes value is
    NOT a sequence for this purpose (iterating it would yield characters).
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _as_bytes(value: Any) -> Optional[bytes]:
    """A message's `uint8[]` payload as bytes.

    Covers dynamic-decoder byte sequences, numpy arrays, plain bytes, and the
    two shapes JSON encoders use for
    binary data -- a base64 string (rosbridge) or a list of ints.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None
    try:
        return bytes(bytearray(value))
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    """float(value), or None for anything that is not a finite number.

    NaN/inf are dropped rather than written: Parquet stores them happily,
    but they propagate through every downstream aggregate as NaN, which is
    strictly less informative than a NULL a reader can filter on.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
