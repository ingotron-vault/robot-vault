"""Unit tests for the `parquet_files` registry writes.

`parquet_files` is the lake's inventory: readers resolve paths and columns
from it instead of globbing S3. The processor registers every file it writes. These tests drive
the real `_extract_minimal_topics_to_parquet` against a real MCAP on local
disk, with only the psycopg boundary faked, and assert on the rows it would
insert -- path, robot, topic, kind, timestamp range, row count, schema
version/hash and the columns that are entirely NULL.

The all-NULL assertion is the important one: most of the 67 allowlisted ROS
2 types have no field extractor yet (`extractors.extract_xyz` returns
(None, None, None)), so plenty of topics produce real rows with no numeric
payload at all. The registry has to say so rather than implying otherwise.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcap.writer import Writer as McapWriter

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor import core  # noqa: E402
from processor import parquet_schema  # noqa: E402
from processor.core import (  # noqa: E402
    ProcessorConfig,
    _HashingSink,
    _extract_minimal_topics_to_parquet,
)


# --- psycopg boundary fake --------------------------------------------------
#
# Same approach the query service's tests take (see
# phases/05-query-service/tests/unit/test_error_vs_empty.py): patch
# `psycopg.connect` on the module under test rather than standing up a
# database, so this stays a unit test.

class _FakeCursor:
    def __init__(self, statements):
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        # Only `_pg_record_failure` reads a result back, and it is not
        # exercised here.
        return (1,)


class _FakeConnection:
    def __init__(self, statements):
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._statements)

    def commit(self):
        pass


@pytest.fixture
def captured_sql(monkeypatch):
    """Capture every statement the processor's Postgres writes would issue."""
    statements = []
    monkeypatch.setattr(core.psycopg, "connect", lambda dsn: _FakeConnection(statements))
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/fake")
    return statements


def registry_rows(statements):
    """The parameter tuples of the `parquet_files` upserts, as dicts."""
    fields = [
        "file_path",
        "robot_id",
        "message_type",
        "kind",
        "upload_id",
        "start_timestamp",
        "end_timestamp",
        "row_count",
        "file_size_bytes",
        "schema_version",
        "schema_hash",
        "column_names",
        "all_null_columns",
        # Appended last, matching the INSERT column order
        # (the schema requires the content digest).
        "sha256",
    ]
    return [
        dict(zip(fields, params))
        for sql, params in statements
        if "INSERT INTO parquet_files" in sql
    ]


# --- fixtures ---------------------------------------------------------------

BASE_TS = 1_700_000_000_000_000_000  # 2023-11-14T22:13:20Z


def _write_mcap(path: Path, messages) -> None:
    """`messages` is a list of (schema_name, topic, encoding, payload, ts)."""
    with path.open("wb") as f:
        writer = McapWriter(f)
        writer.start()
        channels = {}
        for schema_name, topic, encoding, payload, ts in messages:
            key = (schema_name, topic)
            if key not in channels:
                schema_id = writer.register_schema(
                    name=schema_name, encoding="jsonschema", data=b"{}"
                )
                channels[key] = writer.register_channel(
                    topic=topic, message_encoding=encoding, schema_id=schema_id
                )
            writer.add_message(
                channel_id=channels[key], log_time=ts, data=payload, publish_time=ts
            )
        writer.finish()


def _local_config(tmp_path) -> ProcessorConfig:
    return ProcessorConfig(
        parquet_root=tmp_path / "parquet",
        topics=[],
    )


def _force_local_s3_off(monkeypatch):
    for var in ("S3_BUCKET", "S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)


# --- tests ------------------------------------------------------------------

def test_every_written_file_is_registered(tmp_path, monkeypatch, captured_sql):
    """One row per per-topic file, so the registry is a complete inventory of
    what was written."""
    _force_local_s3_off(monkeypatch)
    config = _local_config(tmp_path)

    ts_values = [BASE_TS + i * 1_000_000 for i in range(3)]
    mcap_path = tmp_path / "two_topics.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": 0.1, "y": 0.2, "z": 9.8}).encode(),
                ts_values[0],
            ),
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": 0.3, "y": 0.4, "z": 9.7}).encode(),
                ts_values[1],
            ),
            (
                "sensor_msgs/msg/NavSatFix",
                "/robot/gps",
                "json",
                json.dumps({"longitude": 1.0, "latitude": 2.0, "altitude": 3.0}).encode(),
                ts_values[2],
            ),
        ],
    )

    rows_written = _extract_minimal_topics_to_parquet(
        config, "upload_reg", "robot_reg", str(mcap_path),
        attempt_id="00000000-0000-0000-0000-0000000000a1",
    )
    assert rows_written == 3

    rows = registry_rows(captured_sql)
    by_type = {r["message_type"]: r for r in rows}
    # "gps/fix" is what `supported_types.ROS2_TYPE_NORMALIZATION` maps
    # sensor_msgs/msg/NavSatFix to, i.e. the registry records the same
    # normalized topic the per-topic path is built from.
    # No combined `_all` copy: every row is in exactly one per-topic file.
    assert sorted(by_type) == ["gps/fix", "imu"]
    assert sum(r["row_count"] for r in rows) == 3

    imu = by_type["imu"]
    assert imu["kind"] == "topic"
    assert imu["row_count"] == 2
    assert imu["robot_id"] == "robot_reg"
    assert imu["upload_id"] == "upload_reg"
    assert imu["file_path"].endswith(
        "robot_reg/_attempts/00000000-0000-0000-0000-0000000000a1/"
        "imu/2023/11/14/rows-upload_reg.parquet"
    )

    # Schema version/hash mirror what was stamped into the file itself, and
    # the column list is recorded so a reader never has to assume one.
    for row in rows:
        assert row["schema_version"] == parquet_schema.SCHEMA_VERSION
        assert row["schema_hash"] == parquet_schema.schema_hash()
        assert row["column_names"] == ["ts", "topic", "x", "y", "z"]
        assert row["file_size_bytes"] > 0


def test_registered_sha256_is_the_digest_of_the_file_on_disk(tmp_path, monkeypatch, captured_sql):
    """`parquet_files.sha256` (db/migrations/001_baseline.sql) must be the digest of the
    bytes that actually landed, for every file the processor writes.

    The processor computes it by teeing the write through `hashlib`, never by
    re-reading the object afterwards -- so this test re-reads it, which is the
    only independent way to prove the tee is over the right bytes. A tee that
    silently missed the footer, or double-counted a row group, would produce a
    plausible-looking 64-hex value that matches nothing.
    """
    import hashlib

    _force_local_s3_off(monkeypatch)
    config = _local_config(tmp_path)

    mcap_path = tmp_path / "digest.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": float(i), "y": 0.0, "z": 9.8}).encode(),
                BASE_TS + i * 1_000_000,
            )
            for i in range(4)
        ]
        + [
            (
                "sensor_msgs/msg/NavSatFix",
                "/robot/gps",
                "json",
                json.dumps({"longitude": 1.0, "latitude": 2.0, "altitude": 3.0}).encode(),
                BASE_TS + 9_000_000,
            )
        ],
    )

    _extract_minimal_topics_to_parquet(config, "upload_digest", "robot_digest", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

    rows = registry_rows(captured_sql)
    assert rows, "no parquet_files rows were registered"
    for row in rows:
        recorded = row["sha256"]
        assert recorded is not None, f"{row['message_type']} registered without a digest"
        assert len(recorded) == 64 and recorded == recorded.lower()
        on_disk = hashlib.sha256(Path(row["file_path"]).read_bytes()).hexdigest()
        assert recorded == on_disk, row["message_type"]
        # Consistency with the size recorded from the same write.
        assert row["file_size_bytes"] == Path(row["file_path"]).stat().st_size

    # Two different files must not collide on the digest -- a sink that shared
    # one hasher across destinations would produce identical values here.
    digests = {r["message_type"]: r["sha256"] for r in rows}
    assert len(set(digests.values())) == len(digests), digests


class _WriteProbe:
    """Write-only stream used to prove hashing never needs a read-back pass."""

    def __init__(self, *, result=None):
        self.blocks = []
        self.closed = False
        self.result = result

    def write(self, block):
        self.blocks.append(bytes(block))
        return len(block) if self.result is None else self.result

    def close(self):
        self.closed = True


def test_hashing_sink_streams_exact_bytes_and_only_finalizes_on_close():
    """The registry digest comes from the write stream, never a file re-read."""
    import hashlib

    output = _WriteProbe()
    sink = _HashingSink(output)

    assert sink.write(b"PAR1") == 4
    assert sink.write(memoryview(b"payload-PAR1")) == 12
    assert sink.tell() == 16
    with pytest.raises(RuntimeError, match="before output was finalized"):
        sink.hexdigest()

    sink.close()
    assert b"".join(output.blocks) == b"PAR1payload-PAR1"
    assert sink.hexdigest() == hashlib.sha256(b"PAR1payload-PAR1").hexdigest()


def test_hashing_sink_fails_closed_on_a_short_write():
    """Never register a digest that covers bytes the destination rejected."""
    import hashlib

    output = _WriteProbe(result=3)
    sink = _HashingSink(output)

    with pytest.raises(OSError, match="short parquet write"):
        sink.write(b"PAR1")
    assert sink.tell() == 0
    with pytest.raises(RuntimeError, match="before output was finalized"):
        sink.hexdigest()

    # A caller may recover by redirecting/retrying the next block.  The bytes
    # from the failed short write must not have polluted the digest state.
    output.result = None
    assert sink.write(b"good") == 4
    sink.close()
    assert sink.hexdigest() == hashlib.sha256(b"good").hexdigest()


def test_short_write_prevents_parquet_registry_upsert_end_to_end(
    tmp_path, monkeypatch, captured_sql
):
    """Extraction cannot publish registry evidence for a short output write."""
    _force_local_s3_off(monkeypatch)
    config = _local_config(tmp_path)
    mcap_path = tmp_path / "short-write.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": 1.0, "y": 0.0, "z": 9.8}).encode(),
                BASE_TS,
            )
        ],
    )

    real_hashing_sink = core._HashingSink
    short_output = _WriteProbe(result=3)

    def force_short_output(stream):
        # Close the real destination opened by the production helper, then
        # substitute a stream whose first write is observably short.
        stream.close()
        return real_hashing_sink(short_output)

    monkeypatch.setattr(core, "_HashingSink", force_short_output)

    with pytest.raises(OSError, match="short parquet write"):
        _extract_minimal_topics_to_parquet(
            config, "upload_short", "robot_short", str(mcap_path),
            attempt_id="00000000-0000-0000-0000-0000000000a1",
        )

    assert registry_rows(captured_sql) == []
    assert not any("INSERT INTO parquet_files" in sql for sql, _ in captured_sql)


def test_written_parquet_is_still_readable_through_the_hashing_sink(tmp_path, monkeypatch, captured_sql):
    """The tee sits between the Parquet writer and the output stream, so the
    file's own validity -- footer offsets, key-value metadata, row groups -- is
    the thing most at risk from getting it wrong."""
    import pyarrow.parquet as pq

    _force_local_s3_off(monkeypatch)
    config = _local_config(tmp_path)

    mcap_path = tmp_path / "readable.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": float(i), "y": 1.0, "z": 2.0}).encode(),
                BASE_TS + i * 1_000_000,
            )
            for i in range(5)
        ],
    )
    _extract_minimal_topics_to_parquet(config, "upload_read", "robot_read", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

    imu = {r["message_type"]: r for r in registry_rows(captured_sql)}["imu"]
    table = pq.read_table(imu["file_path"])
    assert table.num_rows == 5
    assert table.schema.names == ["ts", "topic", "x", "y", "z"]
    # The schema-version stamp still rides in the file's key-value metadata.
    assert table.schema.metadata is not None
    assert any(b"schema_version" in key for key in table.schema.metadata)


def test_registered_ts_range_is_the_files_real_min_and_max(tmp_path, monkeypatch, captured_sql):
    """The range has to come from the data, not from "now" or from the
    first row -- it is what a reader prunes on."""
    _force_local_s3_off(monkeypatch)
    config = _local_config(tmp_path)

    # Deliberately out of order, and with a sub-second component, so a
    # first/last-row shortcut or a seconds-only conversion would fail.
    ts_values = [BASE_TS + 5_000_000, BASE_TS + 1_500_000, BASE_TS + 9_250_000]
    mcap_path = tmp_path / "range.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": float(i), "y": 0.0, "z": 0.0}).encode(),
                ts,
            )
            for i, ts in enumerate(ts_values)
        ],
    )

    _extract_minimal_topics_to_parquet(config, "upload_range", "robot_range", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

    imu = {r["message_type"]: r for r in registry_rows(captured_sql)}["imu"]
    expected_min = datetime.fromtimestamp(BASE_TS // 1_000_000_000, tz=timezone.utc).replace(
        microsecond=1500
    )
    expected_max = datetime.fromtimestamp(BASE_TS // 1_000_000_000, tz=timezone.utc).replace(
        microsecond=9250
    )
    assert imu["start_timestamp"] == expected_min
    assert imu["end_timestamp"] == expected_max
    assert imu["row_count"] == 3


def test_all_null_topic_is_registered_as_all_null(tmp_path, monkeypatch, captured_sql):
    """A supported ROS 2 type with no field extractor produces real rows
    whose x/y/z are all NULL. The registry must report that, not imply the
    file carries plottable values."""
    _force_local_s3_off(monkeypatch)
    config = _local_config(tmp_path)

    # tf2_msgs/msg/TFMessage is in the allowlist (supported_types.py) and
    # `extractors.extract_xyz` has no branch for it -> (None, None, None).
    mcap_path = tmp_path / "null_topic.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "tf2_msgs/msg/TFMessage",
                "/tf",
                "json",
                json.dumps({"transforms": []}).encode(),
                BASE_TS,
            ),
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": 1.0, "y": 2.0, "z": 3.0}).encode(),
                BASE_TS + 1_000_000,
            ),
        ],
    )

    _extract_minimal_topics_to_parquet(config, "upload_null", "robot_null", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

    by_type = {r["message_type"]: r for r in registry_rows(captured_sql)}
    tf_row = by_type["tf"]
    assert tf_row["row_count"] == 1
    assert tf_row["all_null_columns"] == ["x", "y", "z"]

    # The topic that does have an extractor is not tarred with the same brush.
    assert by_type["imu"]["all_null_columns"] == []


def test_no_registry_write_without_a_database(tmp_path, monkeypatch):
    """Same guard every other Postgres write in `core.py` has: no
    DATABASE_URL means the processor runs in local/offline mode rather than
    failing."""
    _force_local_s3_off(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def _explode(dsn):
        raise AssertionError("psycopg.connect must not be called without DATABASE_URL")

    monkeypatch.setattr(core.psycopg, "connect", _explode)

    config = _local_config(tmp_path)
    mcap_path = tmp_path / "offline.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": 1.0, "y": 2.0, "z": 3.0}).encode(),
                BASE_TS,
            )
        ],
    )
    assert _extract_minimal_topics_to_parquet(config, "u", "r", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1") == 1


def test_registry_write_failure_is_not_swallowed(tmp_path, monkeypatch):
    """A registry row that silently fails to land would leave files nothing
    can find while the upload is marked processed -- exactly the silent-loss
    class that is already closed for `processed`/`processed_topics`."""
    _force_local_s3_off(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/fake")

    import psycopg

    def _raise_connect(dsn):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(core.psycopg, "connect", _raise_connect)

    config = _local_config(tmp_path)
    mcap_path = tmp_path / "boom.mcap"
    _write_mcap(
        mcap_path,
        [
            (
                "sensor_msgs/msg/Imu",
                "/robot/imu",
                "json",
                json.dumps({"x": 1.0, "y": 2.0, "z": 3.0}).encode(),
                BASE_TS,
            )
        ],
    )

    with pytest.raises(psycopg.OperationalError):
        _extract_minimal_topics_to_parquet(config, "u", "r", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")
