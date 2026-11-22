"""Unit tests for incremental, row-group-flushing Parquet writes.

`_extract_minimal_topics_to_parquet` must not accumulate every extracted row
in Python lists and convert to Arrow only after the ENTIRE MCAP has been
read: peak memory would scale with total message count regardless of
object size (see the `MCAP_READ_BUFFER_BYTES`/`PARQUET_FLUSH_ROWS` comments
in `core.py` for the measured ~425-650 B/row). `_IncrementalParquetSink`
avoids that: one sink per topic,
each flushing a Parquet row group and clearing its buffer every
`PARQUET_FLUSH_ROWS` rows instead of once at the end.

These tests exercise the trap that design has to avoid deliberately: a
`pyarrow.parquet.ParquetWriter`'s schema is fixed by whatever it is opened
with, and every later `write_table()` call must match it exactly. A batch
built by *inferring* types from Python values (e.g. `pa.Table.from_pydict`)
would infer an all-None column as `null`-typed, and a later batch with real
typed values then fails against that writer outright -- verified directly
against pyarrow ("Table schema does not match
schema used to create file"). `_IncrementalParquetSink` never infers: every
batch is built with `pa.array(col, type=<fixed type>)` against the same
explicit `arrow_schema(SCHEMA_VERSION)` the writer itself is opened with, so
an all-None batch is exactly as valid as a fully-populated one for the same
open writer.
"""
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from mcap.writer import Writer as McapWriter

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor import core  # noqa: E402
from processor.core import ProcessorConfig, _extract_minimal_topics_to_parquet  # noqa: E402

BASE_TS = 1_700_000_000_000_000_000  # 2023-11-14T22:13:20Z
ATTEMPT = "00000000-0000-0000-0000-0000000000a1"


def _attempt_root(config, robot_id):
    return config.parquet_root / robot_id / "_attempts" / ATTEMPT


def _write_mcap(path: Path, messages) -> None:
    """`messages` is a list of (schema_name, topic, encoding, payload_obj, ts).

    Same shape/convention as `test_parquet_registry_writes.py::_write_mcap`.
    """
    with path.open("wb") as f:
        writer = McapWriter(f)
        writer.start()
        channels = {}
        for schema_name, topic, encoding, payload_obj, ts in messages:
            key = (schema_name, topic)
            if key not in channels:
                schema_id = writer.register_schema(
                    name=schema_name, encoding="jsonschema", data=b"{}"
                )
                channels[key] = writer.register_channel(
                    topic=topic, message_encoding=encoding, schema_id=schema_id
                )
            writer.add_message(
                channel_id=channels[key],
                log_time=ts,
                data=json.dumps(payload_obj).encode("utf-8"),
                publish_time=ts,
            )
        writer.finish()


def _imu_msg(topic: str, ts: int, reading: dict | None = None):
    """A JSON-encoded sensor_msgs/msg/Imu message.

    `reading`, if given, is a `linear_acceleration` dict and decodes to real
    x/y/z floats. If omitted, the payload is `{}` -- a structurally valid
    message that legitimately extracts to (None, None, None): `extractors.
    _extract_from_json` finds no `linear_acceleration` key and falls through
    to its final `return (None, None, None)`. This is a genuine "no data for
    this row" case (like the 61-of-67 allowlisted types with no field
    extractor at all), not a decode failure -- which is exactly the shape
    that must not corrupt a later, differently-shaped row group in the same
    file.
    """
    payload = {"linear_acceleration": reading} if reading is not None else {}
    return ("sensor_msgs/msg/Imu", topic, "json", payload, ts)


def _config(tmp_path) -> ProcessorConfig:
    return ProcessorConfig(parquet_root=tmp_path / "parquet", topics=[])


def _force_local_mode(monkeypatch):
    """Local disk, no S3, no Postgres dual-write -- regardless of env."""
    for var in (
        "S3_BUCKET", "S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _read_all_columns(root):
    """Read every per-topic file under `root` as plain Python values, in
    (ts, topic) order.

    `ts` is cast to int64 first: converting a nanosecond-resolution Arrow
    timestamp straight to Python datetimes needs pandas (not a dependency
    here) and raises otherwise -- same reason `TestGoldenMcapParquet` in
    test_processing_core.py casts before comparing.
    """
    t = pa.concat_tables(
        [pq.read_table(path) for path in sorted(Path(root).rglob("*.parquet"))]
    )
    t = t.sort_by([("ts", "ascending"), ("topic", "ascending")])
    return {
        "ts": t.column("ts").cast(pa.int64()).to_pylist(),
        "topic": t.column("topic").to_pylist(),
        "x": t.column("x").to_pylist(),
        "y": t.column("y").to_pylist(),
        "z": t.column("z").to_pylist(),
    }


# --- empty / below-threshold inputs ---------------------------------------

def test_empty_input_writes_no_files(tmp_path, monkeypatch):
    _force_local_mode(monkeypatch)
    config = _config(tmp_path)
    mcap_path = tmp_path / "empty.mcap"
    _write_mcap(mcap_path, [])

    rows = _extract_minimal_topics_to_parquet(config, "u_empty", "robot_empty", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

    assert rows == 0
    # No sink was ever created for a zero-row upload, so no
    # robot directory -- let alone a Parquet file -- exists.
    assert not (config.parquet_root / "robot_empty").exists()


def test_single_row_group_below_threshold(tmp_path, monkeypatch):
    """An upload whose row count never reaches PARQUET_FLUSH_ROWS is written
    as exactly one row group, at close() -- the same shape `pq.write_table`
    produces for a whole file."""
    _force_local_mode(monkeypatch)
    config = _config(tmp_path)  # default PARQUET_FLUSH_ROWS (50_000)

    messages = [
        _imu_msg("/robot/imu", BASE_TS + i * 1_000_000, {"x": float(i), "y": 0.0, "z": 0.0})
        for i in range(5)
    ]
    mcap_path = tmp_path / "small.mcap"
    _write_mcap(mcap_path, messages)

    rows = _extract_minimal_topics_to_parquet(config, "u_small", "robot_small", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")
    assert rows == 5

    topic_path = next((_attempt_root(config, "robot_small") / "imu").rglob("*.parquet"))
    assert pq.ParquetFile(topic_path).num_row_groups == 1


# --- multi-row-group structure once the threshold is exceeded --------------

def test_multi_row_group_when_input_exceeds_threshold(tmp_path, monkeypatch):
    _force_local_mode(monkeypatch)
    monkeypatch.setattr(core, "PARQUET_FLUSH_ROWS", 3)
    config = _config(tmp_path)

    n = 10
    messages = [
        _imu_msg("/robot/imu", BASE_TS + i * 1_000_000, {"x": float(i), "y": 0.0, "z": 0.0})
        for i in range(n)
    ]
    mcap_path = tmp_path / "many.mcap"
    _write_mcap(mcap_path, messages)

    rows = _extract_minimal_topics_to_parquet(config, "u_many", "robot_many", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")
    assert rows == n

    topic_path = next((_attempt_root(config, "robot_many") / "imu").rglob("*.parquet"))

    topic_pf = pq.ParquetFile(topic_path)
    # 10 rows at a flush threshold of 3: three full 3-row flushes during
    # add(), plus a final 1-row flush at close() -> 4 row groups.
    assert topic_pf.num_row_groups == 4
    assert topic_pf.read().num_rows == n
    # No rows lost or duplicated across the row-group boundary.
    assert sorted(topic_pf.read().column("x").to_pylist()) == [float(i) for i in range(n)]


# --- THE TRAP: schema consistency across a row-group boundary --------------

def test_schema_transition_all_none_batch_then_typed_batch(tmp_path, monkeypatch):
    """An early-flushed row group with x/y/z all None must not corrupt (or
    fail to write) a later row group in the SAME file that has real float
    values."""
    _force_local_mode(monkeypatch)
    monkeypatch.setattr(core, "PARQUET_FLUSH_ROWS", 3)
    config = _config(tmp_path)

    topic = "/robot/imu"
    # Row group 1 (flushed mid-stream, all-None x/y/z):
    none_batch = [_imu_msg(topic, BASE_TS + i * 1_000_000) for i in range(3)]
    # Row group 2 (flushed at close(), real floats):
    typed_batch = [
        _imu_msg(topic, BASE_TS + (3 + i) * 1_000_000,
                 {"x": float(i), "y": float(i) * 2, "z": float(i) * 3})
        for i in range(3)
    ]
    mcap_path = tmp_path / "transition_none_then_typed.mcap"
    _write_mcap(mcap_path, none_batch + typed_batch)

    rows = _extract_minimal_topics_to_parquet(config, "u_trans1", "robot_trans1", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")
    assert rows == 6

    topic_path = next((_attempt_root(config, "robot_trans1") / "imu").rglob("*.parquet"))
    pf = pq.ParquetFile(topic_path)
    assert pf.num_row_groups == 2
    table = pf.read()
    assert table.column("x").to_pylist() == [None, None, None, 0.0, 1.0, 2.0]
    assert table.column("y").to_pylist() == [None, None, None, 0.0, 2.0, 4.0]
    assert table.column("z").to_pylist() == [None, None, None, 0.0, 3.0, 6.0]


def test_schema_transition_typed_batch_then_all_none_batch(tmp_path, monkeypatch):
    """The reverse ordering: a typed first row group followed by an
    all-None one must be equally unremarkable."""
    _force_local_mode(monkeypatch)
    monkeypatch.setattr(core, "PARQUET_FLUSH_ROWS", 3)
    config = _config(tmp_path)

    topic = "/robot/imu"
    typed_batch = [
        _imu_msg(topic, BASE_TS + i * 1_000_000,
                 {"x": float(i), "y": float(i) * 2, "z": float(i) * 3})
        for i in range(3)
    ]
    none_batch = [_imu_msg(topic, BASE_TS + (3 + i) * 1_000_000) for i in range(3)]
    mcap_path = tmp_path / "transition_typed_then_none.mcap"
    _write_mcap(mcap_path, typed_batch + none_batch)

    rows = _extract_minimal_topics_to_parquet(config, "u_trans2", "robot_trans2", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")
    assert rows == 6

    topic_path = next((_attempt_root(config, "robot_trans2") / "imu").rglob("*.parquet"))
    pf = pq.ParquetFile(topic_path)
    assert pf.num_row_groups == 2
    table = pf.read()
    assert table.column("x").to_pylist() == [0.0, 1.0, 2.0, None, None, None]
    assert table.column("y").to_pylist() == [0.0, 2.0, 4.0, None, None, None]
    assert table.column("z").to_pylist() == [0.0, 3.0, 6.0, None, None, None]


# --- output equivalence: chunking is an internal detail, invisible outside -

def test_output_equivalent_across_flush_thresholds(tmp_path, monkeypatch):
    """The same multi-topic input, processed once at the default threshold
    (larger than the input -> effectively one whole-buffer flush, same
    shape as one whole-file `pq.write_table` call) and once forced to flush
    every 2 rows, must produce identical row VALUES in the per-topic
    files. Row-group batching must be purely an internal memory-bounding
    detail that never changes what a reader sees.
    """
    _force_local_mode(monkeypatch)

    messages = [
        _imu_msg("/robot/imu", BASE_TS + i * 2_000_000, {"x": float(i), "y": -float(i), "z": 1.5})
        for i in range(7)
    ]
    messages += [
        (
            "sensor_msgs/msg/NavSatFix", "/robot/gps", "json",
            {"longitude": 1.0 + i, "latitude": 2.0 + i, "altitude": 3.0 + i},
            BASE_TS + (100 + i) * 2_000_000,
        )
        for i in range(4)
    ]
    mcap_path = tmp_path / "multi_topic.mcap"
    _write_mcap(mcap_path, messages)

    def _run(flush_rows: int, upload_id: str):
        monkeypatch.setattr(core, "PARQUET_FLUSH_ROWS", flush_rows)
        cfg = _config(tmp_path)
        robot_id = f"robot_equiv_{upload_id}"
        rows = _extract_minimal_topics_to_parquet(cfg, upload_id, robot_id, str(mcap_path), attempt_id=ATTEMPT)
        return rows, _read_all_columns(_attempt_root(cfg, robot_id))

    rows_whole, data_whole = _run(50_000, "u_whole")
    rows_chunked, data_chunked = _run(2, "u_chunked")

    assert rows_whole == rows_chunked == 11
    assert data_whole == data_chunked
