from __future__ import annotations

from typing import Any, Optional

from prometheus_client import Counter

try:
    from mcap_ros2.decoder import DecoderFactory
except ImportError:  # pragma: no cover - exercised only by a broken install
    DecoderFactory = None  # type: ignore[assignment,misc]

# Without this counter, a CDR decode failure would null every x/y/z field
# with zero observability. This counter makes decode failures
# visible per-schema; processor.core additionally tracks a per-upload
# attempted/failed ratio (via extractors.extract_xyz's decode_stats param)
# to flag/fail an upload whose CDR-encoded messages are ~100% undecodable.
CDR_DECODE_FAILURES = Counter(
    "processor_cdr_decode_failures_total",
    "ROS2 CDR message decode failures, labeled by schema name",
    ["schema"],
)

_dynamic_decoder_ok: Optional[bool] = None


def check_cdr_decoder_available() -> bool:
    """Check once whether the pure-Python MCAP ROS 2 decoder is importable.

    The decoder reads the ``ros2msg`` definition embedded in each MCAP; it
    intentionally does not import rclpy, rosidl_runtime_py, or generated ROS
    message packages. A missing dependency is still reported at startup, and
    each CDR record remains a counted failure so the upload-level failure gate
    fails closed rather than silently publishing null observations.
    """
    global _dynamic_decoder_ok
    if _dynamic_decoder_ok is not None:
        return _dynamic_decoder_ok
    _dynamic_decoder_ok = DecoderFactory is not None
    if not _dynamic_decoder_ok:
        print(
            "ERROR: mcap-ros2-support is not importable. CDR-encoded ROS 2 "
            "messages will be counted as decode failures and affected uploads "
            "will fail the decode-ratio gate. Repair the pinned runtime "
            "dependency; do not replace CDR messages with JSON fallbacks."
        )
    return _dynamic_decoder_ok


class McapRos2Decoder:
    """One dynamic CDR decoder for one MCAP reader.

    ``mcap_ros2.decoder.DecoderFactory`` caches generated functions by MCAP
    schema ID. Those IDs are local to a file, so a decoder instance must never
    be shared across uploads: schema ID 1 in a second MCAP can describe a
    different message. The caller therefore creates one instance around one
    ``iter_messages`` pass and supplies the actual ``Schema`` record for every
    CDR payload.
    """

    def __init__(self) -> None:
        self._factory = DecoderFactory() if DecoderFactory is not None else None

    def decode(
        self,
        schema_name: str,
        message_encoding: Optional[str],
        schema: Optional[object],
        data: Optional[bytes],
    ) -> Optional[object]:
        if not data:
            return None
        if self._factory is None or schema is None:
            _count_decode_failure(schema_name)
            return None
        try:
            # DecoderFactory accepts exactly reviewed ros2msg/CDR pairs. It
            # returns None for absent or unsupported schema encodings (such as
            # IDL), and raises for malformed definitions or CDR bytes. Every
            # one is a visible failure, never a permissive fallback.
            decoder = self._factory.decoder_for(message_encoding or "", schema)  # type: ignore[arg-type]
            if decoder is None:
                _count_decode_failure(schema_name)
                return None
            return decoder(data)
        except Exception:
            _count_decode_failure(schema_name)
            return None


def _count_decode_failure(schema_name: str) -> None:
    CDR_DECODE_FAILURES.labels(schema=schema_name or "unknown").inc()


def try_decode_cdr(
    schema_name: str,
    data: Optional[bytes],
    *,
    decoder: Optional[McapRos2Decoder] = None,
    schema: Optional[object] = None,
    message_encoding: Optional[str] = None,
) -> Optional[object]:
    """Decode CDR using the actual schema embedded in the current MCAP.

    A schema name alone is deliberately insufficient: accepting it would make
    a generated ROS package (or a guessed definition) an unreviewed decoding
    authority. Missing schemas, IDL schemas, malformed ``ros2msg`` text and
    corrupt bytes all increment the per-schema failure metric.
    """
    if not data:
        return None
    if decoder is None:
        _count_decode_failure(schema_name)
        return None
    return decoder.decode(schema_name, message_encoding, schema, data)


def get_attr(obj: object, path: str) -> Optional[Any]:
    """Safe nested attribute getter: path like 'twist.twist.linear.x'."""
    cur: Any = obj
    for part in path.split("."):
        try:
            cur = getattr(cur, part)
        except Exception:
            return None
    return cur

