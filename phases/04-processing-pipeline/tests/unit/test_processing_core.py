"""
Unit tests for processing pipeline core functionality.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, Mock
from datetime import datetime, timezone
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.fs as pafs
from mcap.writer import Writer as McapWriter

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor.core import (
    ProcessorConfig,
    list_unprocessed_uploads,
    _extract_minimal_topics_to_parquet,
    _s3_filesystem,
    _s3_enabled,
    _get_pg_conn_str,
    run_once,
)
from processor.extractors import extract_xyz


@pytest.fixture(autouse=True)
def _no_ambient_database_url(monkeypatch):
    """Keep this unit module off any Postgres the surrounding environment
    happens to expose.

    `_get_pg_conn_str()` reads DATABASE_URL at call time, and the processor's
    write paths (such as the `parquet_files` registry upsert) are *enabled* by
    its presence. Some CI jobs export DATABASE_URL; clear it so these unit
    tests, which mock the MCAP reader and assert purely on returned row counts
    and on-disk Parquet, never reach a database. Clearing it once for the
    module means a new test cannot silently acquire the dependency.

    Tests that want a DSN still set one explicitly (patch.dict / setenv) or
    patch `_get_pg_conn_str` directly, and both override this fixture.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)


class TestMCAPProcessing:
    """Test MCAP file processing functionality."""


    def test_segmentation_gate_disables_all_episode_dependent_stages(
        self, tmp_path, monkeypatch
    ):
        """Edition core writes Parquet but no episodes/media/QA, and never
        imports the Pro package that would write them."""
        monkeypatch.setenv("ROBOT_VAULT_EDITION", "core")
        monkeypatch.delenv("SEGMENTATION_ENABLED", raising=False)
        config = ProcessorConfig(parquet_root=tmp_path / "parquet", topics=[])
        schema = Mock(name="schema")
        schema.name = "sensor_msgs/msg/Imu"
        channel = Mock(topic="/robot/imu", message_encoding="json")
        channel.topic = "/robot/imu"
        channel.message_encoding = "json"
        message = Mock(log_time=1_000_000_000, data=b"{}")
        message.log_time = 1_000_000_000
        message.data = b"{}"
        mcap_file = tmp_path / "core-edition.mcap"
        mcap_file.write_bytes(b"dummy")
        stats = {}

        with patch("processor.core.make_reader") as reader, \
             patch("processor.pro_stages.load_pro") as load_pro:
            reader.return_value.iter_messages.return_value = [
                (schema, channel, message)
            ]
            rows = _extract_minimal_topics_to_parquet(
                config, "upload-core", "robot1", str(mcap_file), stats=stats,
                attempt_id="00000000-0000-0000-0000-0000000000a1",
            )

        assert rows == 1
        load_pro.assert_not_called()
        assert stats["episodes"] == 0
        assert stats["stages"]["segmentation"] == {
            "status": "skipped",
            "detail": "disabled",
        }
        assert stats["stages"]["media"]["detail"] == "segmentation_disabled"
        assert stats["stages"]["qa"]["detail"] == "segmentation_disabled"

    def test_topic_filtering(self, tmp_path):
        """Test filtering messages by topic."""
        config = ProcessorConfig(
            parquet_root=tmp_path / "parquet",
            topics=["imu"]  # Only process IMU topics (normalized topic name)
        )
        
        mock_messages = []
        
        # IMU message (should be processed)
        imu_schema = Mock()
        imu_schema.name = "sensor_msgs/msg/Imu"
        imu_channel = Mock()
        imu_channel.topic = "/robot/imu"
        imu_channel.message_encoding = "json"
        imu_msg = Mock()
        imu_msg.log_time = 1000000000
        imu_msg.data = b'{"linear_acceleration":{"x":1,"y":2,"z":3}}'
        mock_messages.append((imu_schema, imu_channel, imu_msg))
        
        # Camera message (should be filtered out)
        cam_schema = Mock()
        cam_schema.name = "sensor_msgs/msg/Image"
        cam_channel = Mock()
        cam_channel.topic = "/robot/camera"
        cam_channel.message_encoding = "cdr"
        cam_msg = Mock()
        cam_msg.log_time = 1000000001
        cam_msg.data = b"image_data"
        mock_messages.append((cam_schema, cam_channel, cam_msg))

        # Create a dummy MCAP file
        mcap_file = tmp_path / "test.mcap"
        mcap_file.write_bytes(b"dummy mcap data")

        with patch('processor.core.make_reader') as mock_reader:
            mock_reader.return_value.iter_messages.return_value = mock_messages

            # Read the real (tiny) per-topic output back to see which
            # topics actually got written.
            rows = _extract_minimal_topics_to_parquet(
                config,
                "upload123",
                "robot1",
                str(mcap_file),
                attempt_id="00000000-0000-0000-0000-0000000000a1",
            )

        assert rows == 1  # Only IMU message processed
        written = list(config.parquet_root.rglob("*.parquet"))
        assert len(written) == 1
        processed_topics = pq.read_table(written[0]).column("topic").to_pylist()
        assert processed_topics == ["imu"]
    
class TestGoldenMcapParquet:
    """Golden-file test.

    Everything below writes a REAL MCAP file to disk with the `mcap`
    writer, runs it through `_extract_minimal_topics_to_parquet`
    completely UNMOCKED (real `make_reader`, real Parquet written to a
    tmp dir on local disk), then reads the Parquet back with pyarrow and
    checks it against the exact values that were written. Tests that mocked
    both `make_reader` and `_s3_filesystem` would only prove that mocks
    return mocks.

    Message encoding choice -- JSON, not CDR:
    a CDR message is valid only with its complete MCAP `ros2msg` schema, and
    this fixture deliberately tests the independent JSON end-to-end path.
    Supplying raw CDR bytes with a placeholder schema would only demonstrate
    the fail-closed decoder and make value assertions meaningless. The
    unconditional schema-bearing CDR round trips live in
    `test_typed_extractors.py`; this fixture instead writes an explicitly JSON
    channel with a `sensor_msgs/msg/Imu` `linear_acceleration` object, then
    proves the real, non-mocked JSON extraction and Parquet write path.
    """

    def _write_golden_mcap(self, path: Path, readings: list[dict], ts_values: list[int],
                            topic: str = "/robot/imu", schema_name: str = "sensor_msgs/msg/Imu") -> None:
        with path.open("wb") as f:
            writer = McapWriter(f)
            writer.start()
            schema_id = writer.register_schema(
                name=schema_name, encoding="jsonschema", data=b"{}",
            )
            channel_id = writer.register_channel(
                topic=topic, message_encoding="json", schema_id=schema_id,
            )
            for reading, ts in zip(readings, ts_values):
                payload = {"linear_acceleration": reading}
                writer.add_message(
                    channel_id=channel_id,
                    log_time=ts,
                    data=json.dumps(payload).encode("utf-8"),
                    publish_time=ts,
                )
            writer.finish()

    def test_real_mcap_to_parquet_roundtrip(self, tmp_path, monkeypatch):
        # Force the local-disk path (not S3) and make sure no real
        # Postgres dual-write is attempted, regardless of what the host/CI
        # environment happens to have set.
        for var in ("S3_BUCKET", "S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL"):
            monkeypatch.delenv(var, raising=False)

        config = ProcessorConfig(
            parquet_root=tmp_path / "parquet",
            topics=[],
        )

        # Distinct, exact float literals (not computed via repeated
        # addition) so the JSON round-trip and Parquet float64 storage can
        # be compared with plain `==` -- no precision fuzz to hide a bug.
        readings = [
            {"x": 0.1, "y": 0.2, "z": 9.8},
            {"x": -1.5, "y": 2.5, "z": 9.75},
            {"x": 0.0, "y": 0.0, "z": 9.81},
            {"x": 3.25, "y": -3.25, "z": 9.79},
        ]
        base_ts = 1_700_000_000_000_000_000
        ts_values = [base_ts + i * 1_000_000 for i in range(len(readings))]  # 1 ms apart

        mcap_path = tmp_path / "golden.mcap"
        self._write_golden_mcap(mcap_path, readings, ts_values)

        stats: dict = {}
        rows = _extract_minimal_topics_to_parquet(
            config, "upload_golden", "robot_golden", str(mcap_path), stats=stats,
            attempt_id="00000000-0000-0000-0000-0000000000a1",
        )

        assert rows == len(readings)
        assert stats["messages_read"] == len(readings)
        assert stats["messages_skipped_by_schema"] == {}

        # The per-topic Parquet file is real, on disk, below the attempt that
        # wrote it. There is no combined `_all` copy.
        (out_path,) = list(
            (config.parquet_root / "robot_golden" / "_attempts").rglob("*.parquet")
        )
        assert out_path.name == "rows-upload_golden.parquet"

        table = pq.read_table(out_path)
        assert table.num_rows == len(readings)
        assert table.column_names == ["ts", "topic", "x", "y", "z"]

        # Cast ts to int64 to compare exact nanosecond values -- going
        # through Python datetime objects (table["ts"].to_pylist()) loses
        # sub-microsecond precision and isn't necessary here.
        got_ts = table.column("ts").cast(pa.int64()).to_pylist()
        got_topic = table.column("topic").to_pylist()
        got_x = table.column("x").to_pylist()
        got_y = table.column("y").to_pylist()
        got_z = table.column("z").to_pylist()

        assert got_ts == ts_values
        # No "ang"/"gyro"/"lin"/"acc" hint in the raw topic "/robot/imu",
        # so _normalize_topic falls through to the plain "imu" bucket.
        assert got_topic == ["imu"] * len(readings)
        assert got_x == [r["x"] for r in readings]
        assert got_y == [r["y"] for r in readings]
        assert got_z == [r["z"] for r in readings]

        # The per-topic partition sits at the documented, date-partitioned
        # path below its attempt.
        dt = datetime.fromtimestamp(ts_values[0] // 1_000_000_000, timezone.utc)
        topic_path = (
            config.parquet_root / "robot_golden" / "_attempts"
            / "00000000-0000-0000-0000-0000000000a1" / "imu"
            / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
            / "rows-upload_golden.parquet"
        )
        assert topic_path == out_path
        topic_table = pq.read_table(topic_path)
        assert topic_table.num_rows == len(readings)
        assert topic_table.column("x").to_pylist() == [r["x"] for r in readings]

    def test_real_empty_mcap_produces_zero_rows(self, tmp_path, monkeypatch):
        """A real (but message-less) MCAP file -- as opposed to a zero-byte
        file -- must still be read successfully (real `make_reader`, no
        mocks) and produce 0 rows without writing any Parquet output."""
        for var in ("S3_BUCKET", "S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL"):
            monkeypatch.delenv(var, raising=False)

        config = ProcessorConfig(
            parquet_root=tmp_path / "parquet",
            topics=[],
        )

        mcap_path = tmp_path / "empty_real.mcap"
        with mcap_path.open("wb") as f:
            writer = McapWriter(f)
            writer.start()
            writer.finish()

        rows = _extract_minimal_topics_to_parquet(config, "upload_empty", "robot_golden", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        assert rows == 0
        assert not (config.parquet_root / "robot_golden" / "_all" / "upload_empty.parquet").exists()


class TestLocalPathValidation:
    """A local (non-`s3://`) `storage_path` that does not exist
    on disk must raise FileNotFoundError, not silently `return 0` (which
    `run_once` would treat as indistinguishable from a legitimate empty
    upload and mark permanently processed). Exercised here fully unmocked
    -- `Path.exists()` is False, so `_extract_minimal_topics_to_parquet`
    never even reaches `make_reader`.

    See also `test_processor_failure_paths.py::TestNonexistentStoragePath`
    for the run_once-level (retry/dead-letter bookkeeping) coverage of the
    same rule; this test additionally pins down that the raised
    error identifies the missing path.
    """

    def test_missing_local_storage_path_raises_with_path_in_message(self, tmp_path):
        config = ProcessorConfig(
            parquet_root=tmp_path / "parquet",
            topics=[],
        )
        missing_path = str(tmp_path / "no_such_upload.mcap")
        assert not Path(missing_path).exists()

        with pytest.raises(FileNotFoundError) as exc_info:
            _extract_minimal_topics_to_parquet(config, "upload_missing", "robot1", missing_path, attempt_id="00000000-0000-0000-0000-0000000000a1")

        assert missing_path in str(exc_info.value)


class TestS3Integration:
    """Test S3 integration for processing."""

    def test_s3_filesystem_creation(self):
        """Creating an S3 filesystem with the credentials the code ACTUALLY
        reads.

        Patches the real env vars (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`)
        and asserts on the constructed filesystem's actual configuration. An
        `fs is not None` assertion would be true even when every credential
        is `None`, so it could not catch a wrong variable name.
        """
        with patch.dict('os.environ', {
            'S3_ENDPOINT': 'http://objectstore:9000',
            'S3_BUCKET': 'test-bucket',
            'AWS_ACCESS_KEY_ID': 'test_key',
            'AWS_SECRET_ACCESS_KEY': 'test_secret',
            'S3_REGION': 'us-west-2',
        }):
            assert _s3_enabled() is True

            fs = _s3_filesystem()
            assert isinstance(fs, pafs.S3FileSystem)
            assert fs.region == 'us-west-2'

    def test_s3_not_enabled_with_old_wrong_env_var_names(self, monkeypatch):
        """The names `S3_ACCESS_KEY`/`S3_SECRET_KEY` must NOT enable S3 --
        only `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` do.
        """
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("S3_ENDPOINT", "http://objectstore:9000")
        monkeypatch.setenv("S3_BUCKET", "test-bucket")
        monkeypatch.setenv("S3_ACCESS_KEY", "test_key")
        monkeypatch.setenv("S3_SECRET_KEY", "test_secret")

        assert _s3_enabled() is False

    def test_process_s3_file(self, tmp_path, monkeypatch):
        """An `s3://` storage_path is parsed to PyArrow's bucket/key form
        and read through the S3 filesystem.

        The file here is a real MCAP and the rows come back decoded. A test
        that mocks `_s3_filesystem` AND `make_reader` and asserts only that
        a mock was called would pass against a reader that never runs, which
        is the failure mode the docstring on `TestGoldenMcapParquet`
        calls out.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        config = ProcessorConfig(
            parquet_root=tmp_path / "parquet",
            topics=[]
        )

        mcap_path = tmp_path / "test.mcap"
        with mcap_path.open("wb") as f:
            writer = McapWriter(f)
            writer.start()
            schema_id = writer.register_schema(
                name="sensor_msgs/msg/Imu", encoding="jsonschema", data=b"{}"
            )
            channel_id = writer.register_channel(
                topic="/imu/data", message_encoding="json", schema_id=schema_id
            )
            for i in range(5):
                writer.add_message(
                    channel_id=channel_id,
                    log_time=1_700_000_000_000_000_000 + i * 1_000_000,
                    data=json.dumps({
                        "linear_acceleration": {"x": float(i), "y": 0.0, "z": 9.8}
                    }).encode(),
                    publish_time=1_700_000_000_000_000_000 + i * 1_000_000,
                )
            writer.finish()

        opened = []

        class _FS:
            def open_input_file(self, path):
                opened.append(path)
                return mcap_path.open("rb")

        with patch('processor.core._s3_filesystem', return_value=_FS()):
            rows = _extract_minimal_topics_to_parquet(
                config, "upload123", "robot1", "s3://bucket/raw/test.mcap",
                attempt_id="00000000-0000-0000-0000-0000000000a1",
            )

        assert opened == ["bucket/raw/test.mcap"], (
            "the s3:// URI must be handed to PyArrow as bucket/key"
        )
        assert rows == 5


class TestDatabaseIntegration:
    """Test database integration for tracking processed files."""
    
    def test_get_postgres_connection_string(self):
        """Test getting PostgreSQL connection string from environment."""
        with patch.dict('os.environ', {
            'DATABASE_URL': 'postgresql://testuser:testpass@localhost:5432/testdb'
        }):
            conn_str = _get_pg_conn_str()
            assert conn_str == 'postgresql://testuser:testpass@localhost:5432/testdb'
    
    def test_list_unprocessed_with_postgres(self):
        """Test listing unprocessed uploads from PostgreSQL."""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=None)
        
        mock_cursor.fetchall.return_value = [
            ("upload1", "robot1", "s3://bucket/upload1.mcap", "00000000-0000-0000-0000-000000000001", 1),
            ("upload2", "robot2", "s3://bucket/upload2.mcap", "00000000-0000-0000-0000-000000000002", 1),
        ]
        
        with patch('psycopg.connect') as mock_connect:
            mock_connect.return_value.__enter__.return_value = mock_conn
            mock_connect.return_value.__exit__.return_value = None
            
            with patch('processor.core._get_pg_conn_str') as mock_get_conn:
                mock_get_conn.return_value = "host=localhost"
                
                unprocessed = list_unprocessed_uploads(limit=10)
        
        assert len(unprocessed) == 2
        assert unprocessed[0][0] == "upload1"
        assert unprocessed[1][1] == "robot2"
    
    def test_mark_processed_in_postgres(self, tmp_path):
        """Test marking uploads as processed in PostgreSQL."""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=None)
        # The `episodes` INSERT reads its generated episode_id back
        # via RETURNING, so a cursor whose fetchone() yields a bare Mock is no
        # longer a faithful stand-in for a real one.
        # The claim lock (`SELECT attempts ... FOR UPDATE`) returns the
        # attempt count; every other read returns a generated id.
        mock_cursor.fetchone.side_effect = lambda: (
            (1,)
            if "FOR UPDATE" in str(mock_cursor.execute.call_args)
            else ("00000000-0000-0000-0000-000000000001",)
        )

        config = ProcessorConfig(
            parquet_root=tmp_path / "parquet",
            topics=[]
        )
        
        with patch('psycopg.connect') as mock_connect:
            mock_connect.return_value.__enter__.return_value = mock_conn
            mock_connect.return_value.__exit__.return_value = None
            
            with patch('processor.core._get_pg_conn_str') as mock_get_conn:
                mock_get_conn.return_value = "host=localhost"
                
                # Create a dummy MCAP file
                mcap_file = tmp_path / "test.mcap"
                mcap_file.write_bytes(b"dummy mcap data")
                
                # Create a mock message to trigger processing
                mock_schema = Mock()
                mock_schema.name = "sensor_msgs/msg/Imu"
                mock_channel = Mock()
                mock_channel.topic = "/robot/imu"
                mock_channel.message_encoding = "json"
                mock_msg = Mock()
                mock_msg.log_time = 1000000000
                mock_msg.data = b'{"linear_acceleration":{"x":1,"y":2,"z":3}}'
                mock_messages = [(mock_schema, mock_channel, mock_msg)]
                
                with patch('processor.core.make_reader') as mock_reader:
                    mock_reader.return_value.iter_messages.return_value = mock_messages

                    # Real (single-row, cheap) Parquet write; nothing in the
                    # call path is intercepted.
                    _extract_minimal_topics_to_parquet(
                        config,
                        "upload123",
                        "robot1",
                        str(mcap_file),
                        attempt_id="00000000-0000-0000-0000-0000000000a1",
                    )
        
        # Should have marked as processed. Asserted across every statement
        # rather than only the last one: a later transaction can follow the
        # `processed_topics` write, and "the final
        # statement happens to be an INSERT" was never the property this test
        # meant to pin.
        mock_cursor.execute.assert_called()
        statements = [str(call).lower() for call in mock_cursor.execute.call_args_list]
        assert any("processed" in statement for statement in statements)


class TestExtractors:
    """Test data extraction from different message types."""
    
    
    def test_extract_xyz_from_pointcloud(self):
        """Test extracting metadata from PointCloud2."""
        # PointCloud2 doesn't have simple x,y,z
        x, y, z = extract_xyz(
            "sensor_msgs/msg/PointCloud2",
            "/robot/pointcloud",
            "cdr",
            b"binary_pointcloud_data"
        )
        
        # Should return None for binary data
        assert x is None
        assert y is None
        assert z is None
    
    def test_extract_xyz_fallback(self):
        """Test fallback for unknown message types."""
        x, y, z = extract_xyz(
            "custom/msg/Unknown",
            "/robot/custom",
            "cdr",
            b"unknown_data"
        )
        
        assert x is None
        assert y is None
        assert z is None


class TestPerformanceOptimization:
    """Test performance optimizations in processing."""
    
    def test_batch_processing(self, tmp_path):
        """Test batch processing of multiple messages."""
        config = ProcessorConfig(
            parquet_root=tmp_path / "parquet",
            topics=[]
        )
        
        # Create many messages
        mock_messages = []
        for i in range(1000):
            schema = Mock()
            schema.name = "sensor_msgs/msg/Imu"
            channel = Mock()
            channel.topic = "/robot/imu"
            channel.message_encoding = "json"
            msg = Mock()
            msg.log_time = 1000000000 + i * 1000
            msg.data = b'{"linear_acceleration":{"x":1,"y":2,"z":3}}'
            mock_messages.append((schema, channel, msg))
        
        # Create a dummy MCAP file
        mcap_file = tmp_path / "large.mcap"
        mcap_file.write_bytes(b"dummy mcap data")

        with patch('processor.core.make_reader') as mock_reader:
            mock_reader.return_value.iter_messages.return_value = mock_messages

            # `pq.write_table` is not in the call path
            # (writes go through `pq.ParquetWriter` incrementally instead)
            # -- 1000 rows is well under PARQUET_FLUSH_ROWS, so this is a
            # single real, cheap row group per file. Check the actual
            # written files rather than an intercepted call.
            rows = _extract_minimal_topics_to_parquet(
                config,
                "upload123",
                "robot1",
                str(mcap_file),
                attempt_id="00000000-0000-0000-0000-0000000000a1",
            )

        assert rows == 1000
        # One per-topic file holds every row; there is no combined copy.
        assert not (config.parquet_root / "robot1" / "_all").exists()
        topic_files = list(
            (config.parquet_root / "robot1" / "_attempts").rglob("*.parquet")
        )
        assert len(topic_files) == 1
        assert pq.read_table(topic_files[0]).num_rows == 1000
    
    # The memory guard for S3 streaming is `tests/unit/test_s3_streaming.py`, which asserts
    # peak allocation does not scale with object size and that no single
    # read materialises the object. Both of those fail against code that
    # reads the whole object.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
