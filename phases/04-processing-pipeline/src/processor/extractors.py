from __future__ import annotations

import json
import math
from typing import Optional, Tuple
from .cdr_decode import McapRos2Decoder, get_attr, try_decode_cdr


def _json_try_parse(data: Optional[bytes]) -> Optional[object]:
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def extract_xyz(
    schema_name: str,
    topic: str,
    message_encoding: Optional[str],
    data: Optional[bytes],
    *,
    decode_stats: Optional[dict] = None,
    cdr_decoder: Optional[McapRos2Decoder] = None,
    schema: Optional[object] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Best-effort extraction of three numeric fields for charting.

    Strategy:
    - Prefer ROS 2 CDR decoding when available and extract known fields by type.
    - Fallback to JSON payloads for dev/test fixtures.
    - For images/pointclouds, return (width, height, None) as metadata to allow row counting/plotting.
    - Otherwise return (None, None, None) when not applicable.

    `decode_stats`, if given a dict, is updated in place with "attempted"/
    "failed" counts for CDR decode attempts (the caller uses this to compute a per-upload decode-failure ratio and
    flag/fail an upload whose CDR-encoded messages are ~100% undecodable).
    Callers that don't need this pass nothing and get the old behavior.
    """
    enc = (message_encoding or "").lower()

    # Prefer CDR decoding when possible (typical ROS 2 bag)
    if enc in ("cdr", "cdr_endian", "cdr_endianness", "ros2"):  # be permissive
        if decode_stats is not None:
            decode_stats["attempted"] = decode_stats.get("attempted", 0) + 1
        msg = try_decode_cdr(
            schema_name, data, decoder=cdr_decoder, schema=schema,
            message_encoding=message_encoding,
        )
        if msg is not None:
            return _extract_from_ros2_msg(schema_name, topic, msg)
        if decode_stats is not None:
            decode_stats["failed"] = decode_stats.get("failed", 0) + 1

    # Fallback: JSON payloads (dev/test)
    obj = _json_try_parse(data) if enc == "json" else None
    if isinstance(obj, dict):
        return _extract_from_json(schema_name, topic, obj)
    return (None, None, None)


def _extract_from_json(schema_name: str, topic: str, obj: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    # Simple case: flat x,y,z
    if all(k in obj for k in ("x", "y", "z")):
        return (_num(obj["x"]), _num(obj["y"]), _num(obj["z"]))

    sname = (schema_name or "").lower()
    tname = (topic or "").lower()

    # sensor_msgs/Imu
    if "sensor_msgs/msg/imu" in sname or ("sensor_msgs" in sname and "imu" in sname):
        if any(k in tname for k in ("ang", "gyro")) and isinstance(obj.get("angular_velocity"), dict):
            av = obj.get("angular_velocity") or {}
            return (
                _num(av.get("x")), _num(av.get("y")), _num(av.get("z")),
            )
        if isinstance(obj.get("linear_acceleration"), dict):
            la = obj.get("linear_acceleration") or {}
            return (
                _num(la.get("x")), _num(la.get("y")), _num(la.get("z")),
            )

    # geometry_msgs/Twist
    if "geometry_msgs/msg/twist" in sname and isinstance(obj.get("linear"), dict):
        lin = obj.get("linear") or {}
        return (_num(lin.get("x")), _num(lin.get("y")), _num(lin.get("z")))

    # nav_msgs/Odometry (prefer linear twist; fallback to pose.position)
    if "nav_msgs/msg/odometry" in sname:
        try:
            lin = obj.get("twist", {}).get("twist", {}).get("linear", {})
            if isinstance(lin, dict):
                return (_num(lin.get("x")), _num(lin.get("y")), _num(lin.get("z")))
        except Exception:
            pass
        try:
            pos = obj.get("pose", {}).get("pose", {}).get("position", {})
            if isinstance(pos, dict):
                return (_num(pos.get("x")), _num(pos.get("y")), _num(pos.get("z")))
        except Exception:
            pass

    # sensor_msgs/NavSatFix: map to lon,lat,alt as x,y,z
    if "sensor_msgs/msg/navsatfix" in sname:
        return (_num(obj.get("longitude")), _num(obj.get("latitude")), _num(obj.get("altitude")))

    # sensor_msgs/MagneticField
    if "sensor_msgs/msg/magneticfield" in sname and isinstance(obj.get("magnetic_field"), dict):
        mf = obj.get("magnetic_field") or {}
        return (_num(mf.get("x")), _num(mf.get("y")), _num(mf.get("z")))

    # geometry_msgs/AccelWithCovarianceStamped
    if "geometry_msgs/msg/accelwithcovariancestamped" in sname:
        try:
            lin = obj.get("accel", {}).get("accel", {}).get("linear", {})
            if isinstance(lin, dict):
                return (_num(lin.get("x")), _num(lin.get("y")), _num(lin.get("z")))
        except Exception:
            pass

    # sensor_msgs/BatteryState: voltage, current, percentage
    if "sensor_msgs/msg/batterystate" in sname:
        return (_num(obj.get("voltage")), _num(obj.get("current")), _num(obj.get("percentage")))

    # sensor_msgs/Temperature
    if "sensor_msgs/msg/temperature" in sname:
        return (_num(obj.get("temperature")), None, None)

    # sensor_msgs/Image → (width, height, None)
    if "sensor_msgs/msg/image" in sname:
        return (_num(obj.get("width")), _num(obj.get("height")), None)

    # sensor_msgs/PointCloud2 → (width, height, None)
    if "sensor_msgs/msg/pointcloud2" in sname:
        return (_num(obj.get("width")), _num(obj.get("height")), None)

    # Fallback
    return (None, None, None)


def _extract_from_ros2_msg(schema_name: str, topic: str, msg: object) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    sname = (schema_name or "").lower()
    tname = (topic or "").lower()

    # sensor_msgs/Imu
    if sname.endswith("/imu"):
        if any(k in tname for k in ("ang", "gyro")):
            return (
                _num(get_attr(msg, "angular_velocity.x")),
                _num(get_attr(msg, "angular_velocity.y")),
                _num(get_attr(msg, "angular_velocity.z")),
            )
        return (
            _num(get_attr(msg, "linear_acceleration.x")),
            _num(get_attr(msg, "linear_acceleration.y")),
            _num(get_attr(msg, "linear_acceleration.z")),
        )

    # geometry_msgs/Twist
    if sname.endswith("/twist"):
        return (
            _num(get_attr(msg, "linear.x")),
            _num(get_attr(msg, "linear.y")),
            _num(get_attr(msg, "linear.z")),
        )

    # nav_msgs/Odometry
    if sname.endswith("/odometry"):
        x = _num(get_attr(msg, "twist.twist.linear.x"))
        y = _num(get_attr(msg, "twist.twist.linear.y"))
        z = _num(get_attr(msg, "twist.twist.linear.z"))
        if x is not None or y is not None or z is not None:
            return (x, y, z)
        return (
            _num(get_attr(msg, "pose.pose.position.x")),
            _num(get_attr(msg, "pose.pose.position.y")),
            _num(get_attr(msg, "pose.pose.position.z")),
        )

    # sensor_msgs/NavSatFix: lon,lat,alt
    if sname.endswith("/navsatfix"):
        return (
            _num(get_attr(msg, "longitude")),
            _num(get_attr(msg, "latitude")),
            _num(get_attr(msg, "altitude")),
        )

    # sensor_msgs/MagneticField
    if sname.endswith("/magneticfield"):
        return (
            _num(get_attr(msg, "magnetic_field.x")),
            _num(get_attr(msg, "magnetic_field.y")),
            _num(get_attr(msg, "magnetic_field.z")),
        )

    # geometry_msgs/AccelWithCovarianceStamped
    if sname.endswith("/accelwithcovariancestamped"):
        return (
            _num(get_attr(msg, "accel.accel.linear.x")),
            _num(get_attr(msg, "accel.accel.linear.y")),
            _num(get_attr(msg, "accel.accel.linear.z")),
        )

    # sensor_msgs/BatteryState: voltage, current, percentage
    if sname.endswith("/batterystate"):
        return (
            _num(get_attr(msg, "voltage")),
            _num(get_attr(msg, "current")),
            _num(get_attr(msg, "percentage")),
        )

    # sensor_msgs/Temperature
    if sname.endswith("/temperature"):
        return (_num(get_attr(msg, "temperature")), None, None)

    # sensor_msgs/Image → (width, height, None)
    if sname.endswith("/image"):
        return (
            _num(get_attr(msg, "width")),
            _num(get_attr(msg, "height")),
            None,
        )

    # sensor_msgs/PointCloud2 → (width, height, None)
    if sname.endswith("/pointcloud2"):
        return (
            _num(get_attr(msg, "width")),
            _num(get_attr(msg, "height")),
            None,
        )

    return (None, None, None)


def _num(v: object) -> Optional[float]:
    """A finite float, or None. NaN and infinities are dropped, as in
    `typed_extractors._float`: one NaN would otherwise turn every ratio built
    from this value into NaN and make the stored JSON invalid."""
    try:
        out = float(v) if v is not None else None
    except Exception:
        return None
    return out if out is not None and math.isfinite(out) else None

