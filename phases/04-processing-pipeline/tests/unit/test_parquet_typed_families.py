"""Unit tests for the v2 typed-family Parquet writes.

The processor writes a second set of files beside the v1
per-topic ones: for the six message types with a typed extractor, a file
per (family, topic) under `processed/parquet/v2/{family}/...`, in that
family's fixed Arrow schema, registered in `parquet_files` with
`schema_version=2`.

Two properties matter most and both are asserted here:

  1. **v1 is untouched.** Same files, same paths, same rows, same
     `rows_written`, same registry rows. The typed files are additional or
     they are a regression.
  2. **The v2 schema is fixed, never inferred.** Same trap
     `test_parquet_incremental_writer.py` documents for v1: a
     `ParquetWriter` fixes its schema on open, so an early all-NULL row
     group must not retype a column out from under a later typed one.
     Here that is exercised across a real flush boundary with
     `PARQUET_FLUSH_ROWS` forced low.

Registration is asserted against the same faked psycopg boundary
`test_parquet_registry_writes.py` uses -- real MCAP, real Parquet on local
disk, only the database mocked.
"""
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from mcap.writer import Writer as McapWriter

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor import core  # noqa: E402
from processor import parquet_schema  # noqa: E402
from processor.core import ProcessorConfig, _extract_minimal_topics_to_parquet  # noqa: E402

BASE_TS = 1_700_000_000_000_000_000  # 2023-11-14T22:13:20Z


# --- psycopg boundary fake (same shape as test_parquet_registry_writes) -----

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
    statements = []
    monkeypatch.setattr(core.psycopg, "connect", lambda dsn: _FakeConnection(statements))
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/fake")
    return statements


REGISTRY_FIELDS = [
    "file_path", "robot_id", "message_type", "kind", "upload_id",
    "start_timestamp", "end_timestamp", "row_count", "file_size_bytes",
    "schema_version", "schema_hash", "column_names", "all_null_columns",
    "sha256",
]


def registry_rows(statements):
    return [
        dict(zip(REGISTRY_FIELDS, params))
        for sql, params in statements
        if "INSERT INTO parquet_files" in sql
    ]


# --- fixtures ---------------------------------------------------------------

def _write_mcap(path: Path, messages) -> None:
    """`messages` is a list of (schema_name, topic, encoding, payload_obj, ts)."""
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


def _config(tmp_path) -> ProcessorConfig:
    return ProcessorConfig(parquet_root=tmp_path / "parquet", topics=[])


def _force_local_mode(monkeypatch):
    for var in (
        "S3_BUCKET", "S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _force_local_s3_off(monkeypatch):
    for var in ("S3_BUCKET", "S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)


def _joint_state(topic: str, ts: int, positions, names=("j0", "j1"), **extra):
    payload = {"name": list(names), "position": list(positions)}
    payload.update(extra)
    return ("sensor_msgs/msg/JointState", topic, "json", payload, ts)


def _odom(topic: str, ts: int, x: float):
    return (
        "nav_msgs/msg/Odometry", topic, "json",
        {
            "pose": {"pose": {
                "position": {"x": x, "y": 0.0, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            }},
            "twist": {"twist": {
                "linear": {"x": 0.5, "y": 0.0, "z": 0.0},
                "angular": {"x": 0.0, "y": 0.0, "z": 0.1},
            }},
        },
        ts,
    )


def _twist(topic: str, ts: int, linear_x: float, angular_z: float):
    return (
        "geometry_msgs/msg/Twist", topic, "json",
        {
            "linear": {"x": linear_x, "y": 0.0, "z": 0.0},
            "angular": {"x": 0.0, "y": 0.0, "z": angular_z},
        },
        ts,
    )


def _tf(topic: str, ts: int, parent: str, child: str):
    return (
        "tf2_msgs/msg/TFMessage", topic, "json",
        {"transforms": [{
            "header": {"frame_id": parent},
            "child_frame_id": child,
            "transform": {
                "translation": {"x": 1.0, "y": 2.0, "z": 3.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            },
        }]},
        ts,
    )


def _imu(topic: str, ts: int):
    return (
        "sensor_msgs/msg/Imu", topic, "json",
        {"linear_acceleration": {"x": 0.0, "y": 0.0, "z": 9.8}}, ts,
    )


def _typed_files(config: ProcessorConfig):
    root = config.parquet_root / "v2"
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.parquet"))


# --- layout -----------------------------------------------------------------

class TestLayout:
    def test_typed_files_land_under_the_v2_family_prefix(self, tmp_path, monkeypatch):
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "typed.mcap"
        _write_mcap(mcap_path, [
            _joint_state("/joint_states", BASE_TS, [1.0, 2.0]),
            _odom("/odom", BASE_TS + 1_000_000, 5.0),
            _tf("/tf", BASE_TS + 2_000_000, "odom", "base_link"),
        ])

        _extract_minimal_topics_to_parquet(config, "u_layout", "robot_layout", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        assert _typed_files(config) == [
            "joint_state/robot_layout/_attempts/00000000-0000-0000-0000-0000000000a1/joint_state/rows-u_layout.parquet",
            "odometry/robot_layout/_attempts/00000000-0000-0000-0000-0000000000a1/odom/rows-u_layout.parquet",
            "tf/robot_layout/_attempts/00000000-0000-0000-0000-0000000000a1/tf/rows-u_layout.parquet",
        ]

    def test_v1_layout_is_unchanged_by_the_typed_writes(self, tmp_path, monkeypatch):
        """The regression the typed writes must never cause: every v1 file
        stays exactly where it was, with exactly the rows it had."""
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "mixed.mcap"
        _write_mcap(mcap_path, [
            _joint_state("/joint_states", BASE_TS, [1.0, 2.0]),
            _imu("/robot/imu", BASE_TS + 1_000_000),
        ])

        rows = _extract_minimal_topics_to_parquet(config, "u_v1", "robot_v1", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        # rows_written stays the v1 MESSAGE count -- the JointState message
        # produced two typed rows, and none of them may leak into this.
        assert rows == 2

        v1_written = sorted(
            str(p.relative_to(config.parquet_root))
            for p in config.parquet_root.rglob("*.parquet")
            if not str(p.relative_to(config.parquet_root)).startswith("v2/")
        )
        assert v1_written == [
            "robot_v1/_attempts/00000000-0000-0000-0000-0000000000a1/imu/2023/11/14/rows-u_v1.parquet",
            "robot_v1/_attempts/00000000-0000-0000-0000-0000000000a1/joint_state/2023/11/14/rows-u_v1.parquet",
        ]
        tables = [
            pq.read_table(config.parquet_root / path) for path in v1_written
        ]
        assert all(t.column_names == parquet_schema.column_names(1) for t in tables)
        assert sum(t.num_rows for t in tables) == 2

    def test_types_without_a_typed_family_write_no_v2_files(self, tmp_path, monkeypatch):
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "imu_only.mcap"
        _write_mcap(mcap_path, [_imu("/robot/imu", BASE_TS)])

        _extract_minimal_topics_to_parquet(config, "u_imu", "robot_imu", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        assert not (config.parquet_root / "v2").exists()

    def test_topics_of_one_family_share_their_normalized_bucket(self, tmp_path, monkeypatch):
        """`/tf` and `/tf_static` normalize to the same `tf` bucket, so they
        land in one file -- and `is_static` is what keeps them apart, which
        is the reason that column exists."""
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "tf_both.mcap"
        _write_mcap(mcap_path, [
            _tf("/tf", BASE_TS, "odom", "base_link"),
            _tf("/tf_static", BASE_TS + 1_000_000, "base_link", "lidar"),
        ])

        _extract_minimal_topics_to_parquet(config, "u_tf", "robot_tf", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        assert _typed_files(config) == ["tf/robot_tf/_attempts/00000000-0000-0000-0000-0000000000a1/tf/rows-u_tf.parquet"]
        table = pq.read_table(
            config.parquet_root / "v2" / "tf" / "robot_tf" / "_attempts" / "00000000-0000-0000-0000-0000000000a1" / "tf" / "rows-u_tf.parquet"
        )
        assert table.column("child_frame").to_pylist() == ["base_link", "lidar"]
        assert table.column("is_static").to_pylist() == [False, True]

    def test_typed_filenames_are_not_matchable_by_the_legacy_part_globs(
        self, tmp_path, monkeypatch
    ):
        """`part-*.parquet` is the filename the query service's fallback
        globs match. A typed file named `part-*` would be swept into a v1
        query on that path and double-count."""
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "names.mcap"
        _write_mcap(mcap_path, [_joint_state("/joint_states", BASE_TS, [1.0, 2.0])])

        _extract_minimal_topics_to_parquet(config, "u_names", "robot_names", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        typed = list((config.parquet_root / "v2").rglob("*.parquet"))
        assert typed and all(p.name.startswith("rows-") for p in typed)
        assert not list((config.parquet_root / "v2").rglob("part-*.parquet"))

    def test_s3_keys_stay_inside_the_swept_and_derived_gc_prefix(self, monkeypatch):
        """GC derivation must cover every prefix the processor writes to.
        `tools/s3_gc.py` derives reachability from `parquet_files.
        file_path`, which the processor writes for the typed files too, but
        that only holds if the key is (a) normalizable to the bare form the GC
        compares against, and (b) inside a prefix the GC actually sweeps.
        Assert both, so a future layout change cannot quietly park objects
        outside the sweep (unreachable-but-undeleted) or inside it with an
        underivable key (deletable).
        """
        monkeypatch.setenv("S3_BUCKET", "robotics-data")
        root = core._s3_parquet_root(2, family="joint_state")
        assert root == "robotics-data/processed/parquet/v2/joint_state"

        file_path = f"{root}/robot_a/joint_state/rows-u1.parquet"
        # The real GC module, not a copy of its rules -- a reimplementation
        # here could agree with itself while disagreeing with the script
        # that actually deletes. It pulls in boto3, which the dev image
        # (where the unit gate runs) has and the minimal processor image does
        # not.
        pytest.importorskip("boto3", reason="tools/s3_gc.py needs boto3")
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools"))
        import s3_gc  # noqa: E402  (tools/s3_gc.py, the only GC in the repo)

        key = s3_gc.normalize_object_key(file_path, "robotics-data")
        assert key == "processed/parquet/v2/joint_state/robot_a/joint_state/rows-u1.parquet"
        assert key.startswith("processed/parquet/")

    def test_topic_path_segment_is_flattened(self):
        """One directory per topic, whatever depth the topic name has --
        the v2 layout spends its depth on version/family/robot."""
        assert core._sanitize_path_segment("joint_state") == "joint_state"
        assert core._sanitize_path_segment("imu/linear_accel") == "imu__linear_accel"
        assert core._sanitize_path_segment("/leading/slash/") == "leading__slash"
        assert core._sanitize_path_segment("") == "_unknown"


# --- contents ---------------------------------------------------------------

class TestContents:
    def test_joint_state_is_written_long_one_row_per_joint(self, tmp_path, monkeypatch):
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "joints.mcap"
        _write_mcap(mcap_path, [
            _joint_state("/joint_states", BASE_TS, [1.0, 2.0], velocity=[0.1, 0.2]),
            _joint_state("/joint_states", BASE_TS + 1_000_000, [3.0, 4.0], velocity=[0.3, 0.4]),
        ])

        _extract_minimal_topics_to_parquet(config, "u_joints", "robot_joints", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        path = (
            config.parquet_root / "v2" / "joint_state" / "robot_joints" / "_attempts" / "00000000-0000-0000-0000-0000000000a1"
            / "joint_state" / "rows-u_joints.parquet"
        )
        table = pq.read_table(path)
        assert table.column_names == parquet_schema.column_names(2, "joint_state")
        assert table.num_rows == 4  # 2 messages x 2 joints
        assert table.column("joint_name").to_pylist() == ["j0", "j1", "j0", "j1"]
        assert table.column("position").to_pylist() == [1.0, 2.0, 3.0, 4.0]
        assert table.column("velocity").to_pylist() == [0.1, 0.2, 0.3, 0.4]
        assert table.column("effort").to_pylist() == [None] * 4
        # The topic column carries the NORMALIZED bucket, exactly as v1's does.
        assert set(table.column("topic").to_pylist()) == {"joint_state"}
        assert table.column("ts").cast(pa.int64()).to_pylist() == [
            BASE_TS, BASE_TS, BASE_TS + 1_000_000, BASE_TS + 1_000_000,
        ]

    def test_odometry_keeps_pose_and_twist(self, tmp_path, monkeypatch):
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "odom.mcap"
        _write_mcap(mcap_path, [_odom("/odom", BASE_TS, 7.5)])

        _extract_minimal_topics_to_parquet(config, "u_odom", "robot_odom", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        table = pq.read_table(
            config.parquet_root / "v2" / "odometry" / "robot_odom" / "_attempts" / "00000000-0000-0000-0000-0000000000a1" / "odom"
            / "rows-u_odom.parquet"
        )
        assert table.num_rows == 1
        row = {name: table.column(name).to_pylist()[0] for name in table.column_names}
        assert row["px"] == 7.5
        assert row["qw"] == 1.0
        assert row["vx"] == 0.5
        assert row["wz"] == 0.1

    def test_twist_keeps_the_angular_command_v1_drops(self, tmp_path, monkeypatch):
        """The typed Twist family preserves the angular command v1 omits.

        The v1 file carries `(x, y, z) = linear.{x,y,z}` and nothing else.
        The typed file beside it carries all six components, while v1 remains
        unchanged for every reader that already depends on it. This is the
        additive-family contract in the phase 04 technical guide.
        """
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "cmd.mcap"
        _write_mcap(mcap_path, [
            _twist("/cmd_vel", BASE_TS, 0.4, -0.75),
            _twist("/cmd_vel", BASE_TS + 1_000_000, 0.5, 0.25),
        ])

        _extract_minimal_topics_to_parquet(config, "u_cmd", "robot_cmd", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        typed = pq.read_table(
            config.parquet_root / "v2" / "twist" / "robot_cmd" / "_attempts" / "00000000-0000-0000-0000-0000000000a1" / "twist"
            / "rows-u_cmd.parquet"
        )
        assert typed.column_names == parquet_schema.column_names(2, "twist")
        assert typed.num_rows == 2  # one row per message, not per component
        assert typed.column("linear_x").to_pylist() == [0.4, 0.5]
        assert typed.column("angular_z").to_pylist() == [-0.75, 0.25]
        assert set(typed.column("topic").to_pylist()) == {"twist"}

        v1 = pq.read_table(
            config.parquet_root / "robot_cmd" / "_attempts" / "00000000-0000-0000-0000-0000000000a1" / "twist"
            / "2023" / "11" / "14" / "rows-u_cmd.parquet"
        )
        assert v1.column_names == parquet_schema.column_names(1)
        assert v1.column("x").to_pylist() == [0.4, 0.5]
        # The angular command has nowhere to go in v1 -- z is linear.z.
        assert v1.column("z").to_pylist() == [0.0, 0.0]

    def test_files_are_self_describing(self, tmp_path, monkeypatch):
        """Version alone stops identifying a shape at v2, so the family is
        stamped in too -- a file copied out of the lake still says what it
        is."""
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "meta.mcap"
        _write_mcap(mcap_path, [_joint_state("/joint_states", BASE_TS, [1.0, 2.0])])

        _extract_minimal_topics_to_parquet(config, "u_meta", "robot_meta", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        path = (
            config.parquet_root / "v2" / "joint_state" / "robot_meta" / "_attempts" / "00000000-0000-0000-0000-0000000000a1"
            / "joint_state" / "rows-u_meta.parquet"
        )
        meta = parquet_schema.read_file_metadata(pq.read_schema(path))
        assert meta["schema_version"] == "2"
        assert meta["schema_family"] == "joint_state"
        assert meta["schema_hash"] == parquet_schema.schema_hash(2, "joint_state")
        assert meta["kind"] == "typed"
        assert meta["topic"] == "joint_state"
        assert meta["robot_id"] == "robot_meta"
        assert meta["upload_id"] == "u_meta"


# --- fixed schema across flush boundaries -----------------------------------

class TestFixedSchemaFlush:
    def test_all_null_row_group_then_typed_row_group(self, tmp_path, monkeypatch):
        """The v1 trap, re-run for a family: an early row group whose
        `velocity` is entirely NULL must not retype the column and break
        (or corrupt) a later row group that has real floats."""
        _force_local_mode(monkeypatch)
        monkeypatch.setattr(core, "PARQUET_FLUSH_ROWS", 4)
        config = _config(tmp_path)

        mcap_path = tmp_path / "flush.mcap"
        _write_mcap(mcap_path, [
            # 2 messages x 2 joints = 4 rows, no velocity -> one flushed
            # row group, all-NULL velocity.
            _joint_state("/joint_states", BASE_TS, [1.0, 2.0]),
            _joint_state("/joint_states", BASE_TS + 1_000_000, [3.0, 4.0]),
            # 4 more rows, with velocity -> a second row group.
            _joint_state("/joint_states", BASE_TS + 2_000_000, [5.0, 6.0], velocity=[0.5, 0.6]),
            _joint_state("/joint_states", BASE_TS + 3_000_000, [7.0, 8.0], velocity=[0.7, 0.8]),
        ])

        _extract_minimal_topics_to_parquet(config, "u_flush", "robot_flush", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        path = (
            config.parquet_root / "v2" / "joint_state" / "robot_flush" / "_attempts" / "00000000-0000-0000-0000-0000000000a1"
            / "joint_state" / "rows-u_flush.parquet"
        )
        pf = pq.ParquetFile(path)
        assert pf.num_row_groups == 2
        table = pf.read()
        assert table.schema.field("velocity").type == pa.float64()
        assert table.column("velocity").to_pylist() == [
            None, None, None, None, 0.5, 0.6, 0.7, 0.8,
        ]
        assert table.column("position").to_pylist() == [
            1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0,
        ]

    def test_chunking_is_invisible_to_a_reader(self, tmp_path, monkeypatch):
        """Row-group batching is a memory-bounding detail: the same input at
        two flush thresholds must produce identical values."""
        _force_local_mode(monkeypatch)
        messages = [
            _joint_state("/joint_states", BASE_TS + i * 1_000_000, [float(i), float(-i)],
                         velocity=[0.1 * i, -0.1 * i])
            for i in range(9)
        ]
        mcap_path = tmp_path / "equiv.mcap"
        _write_mcap(mcap_path, messages)

        def _run(flush_rows: int, upload_id: str):
            monkeypatch.setattr(core, "PARQUET_FLUSH_ROWS", flush_rows)
            cfg = _config(tmp_path)
            _extract_minimal_topics_to_parquet(cfg, upload_id, "robot_equiv", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")
            path = (
                cfg.parquet_root / "v2" / "joint_state" / "robot_equiv" / "_attempts" / "00000000-0000-0000-0000-0000000000a1"
                / "joint_state" / f"rows-{upload_id}.parquet"
            )
            table = pq.read_table(path)
            return {
                name: (
                    table.column(name).cast(pa.int64()).to_pylist()
                    if name == "ts" else table.column(name).to_pylist()
                )
                for name in table.column_names
            }

        whole = _run(50_000, "u_whole")
        chunked = _run(3, "u_chunked")
        assert whole == chunked
        assert len(whole["position"]) == 18


# --- registry ---------------------------------------------------------------

class TestRegistryWrites:
    def test_every_typed_file_is_registered_at_schema_version_2(
        self, tmp_path, monkeypatch, captured_sql
    ):
        """Mirrors `test_every_written_file_is_registered` for v2: one row
        per typed file, carrying the family's real column list. This is
        also what makes the files reachable for GC, which derives
        reachability from `parquet_files.file_path`."""
        _force_local_s3_off(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "reg.mcap"
        _write_mcap(mcap_path, [
            _joint_state("/joint_states", BASE_TS, [1.0, 2.0]),
            _odom("/odom", BASE_TS + 1_000_000, 5.0),
            _twist("/cmd_vel", BASE_TS + 2_000_000, 0.4, -0.75),
            _imu("/robot/imu", BASE_TS + 3_000_000),
        ])

        _extract_minimal_topics_to_parquet(config, "u_reg", "robot_reg", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        rows = registry_rows(captured_sql)
        by_type = {r["message_type"]: r for r in rows}

        # v1 rows: one per normalized topic. There is no combined copy.
        assert {"imu", "joint_state", "odom", "twist"} <= set(by_type)
        assert "_all" not in by_type
        for name in ("imu", "joint_state", "odom", "twist"):
            assert by_type[name]["schema_version"] == 1
            assert by_type[name]["column_names"] == ["ts", "topic", "x", "y", "z"]

        joints = by_type["_v2/joint_state/joint_state"]
        assert joints["schema_version"] == 2
        assert joints["kind"] == "topic"
        assert joints["robot_id"] == "robot_reg"
        assert joints["upload_id"] == "u_reg"
        assert joints["row_count"] == 2  # one message, two joints
        assert joints["column_names"] == parquet_schema.column_names(2, "joint_state")
        assert joints["schema_hash"] == parquet_schema.schema_hash(2, "joint_state")
        assert joints["file_size_bytes"] > 0
        assert joints["file_path"].endswith(
            "v2/joint_state/robot_reg/_attempts/00000000-0000-0000-0000-0000000000a1/joint_state/rows-u_reg.parquet"
        )

        odom = by_type["_v2/odometry/odom"]
        assert odom["schema_version"] == 2
        assert odom["column_names"] == parquet_schema.column_names(2, "odometry")
        assert odom["row_count"] == 1

        twist = by_type["_v2/twist/twist"]
        assert twist["schema_version"] == 2
        assert twist["column_names"] == parquet_schema.column_names(2, "twist")
        assert twist["schema_hash"] == parquet_schema.schema_hash(2, "twist")
        assert twist["row_count"] == 1
        # Readers find this file by its prefix, so the registered path
        # has to be the one `parquet_prefix(root, 2, family="twist")` builds.
        assert twist["file_path"].endswith(
            "v2/twist/robot_reg/_attempts/00000000-0000-0000-0000-0000000000a1/twist/rows-u_reg.parquet"
        )
        # ...and the v1 rows for the same messages are still there, untouched.
        assert by_type["twist"]["schema_version"] == 1

        # The IMU has no typed family, so it contributes no v2 row at all.
        assert not any(r["message_type"].startswith("_v2/imu") for r in rows)

    def test_typed_message_types_cannot_collide_with_a_v1_topic_query(
        self, tmp_path, monkeypatch, captured_sql
    ):
        """`query_service/registry.py:list_files` matches
        `message_type = topic OR message_type LIKE 'topic/%'`. If a typed
        file were registered under the bare topic name, a v1 query for
        `joint_state` would pull in rows of a different shape -- for the
        same messages -- and double-count. The reserved `_v2/` namespace
        (like the existing `_all`) is what makes that structurally
        impossible."""
        _force_local_s3_off(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "ns.mcap"
        _write_mcap(mcap_path, [_joint_state("/joint_states", BASE_TS, [1.0, 2.0])])

        _extract_minimal_topics_to_parquet(config, "u_ns", "robot_ns", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        typed = [r for r in registry_rows(captured_sql) if r["schema_version"] == 2]
        assert typed
        for row in typed:
            assert row["message_type"].startswith("_v2/")
            assert row["message_type"] != "joint_state"
            assert not row["message_type"].startswith("joint_state/")

    def test_all_null_payload_columns_are_recorded_per_family(
        self, tmp_path, monkeypatch, captured_sql
    ):
        """Same honesty requirement v1 has: a reader must not have to
        assume a column carries data. `effort` is never published here."""
        _force_local_s3_off(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "nulls.mcap"
        _write_mcap(mcap_path, [
            _joint_state("/joint_states", BASE_TS, [1.0, 2.0], velocity=[0.1, 0.2]),
        ])

        _extract_minimal_topics_to_parquet(config, "u_nulls", "robot_nulls", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        row = {
            r["message_type"]: r for r in registry_rows(captured_sql)
        }["_v2/joint_state/joint_state"]
        assert row["all_null_columns"] == ["effort"]

    def test_typed_writes_add_no_processed_topics_rows(
        self, tmp_path, monkeypatch, captured_sql
    ):
        """`processed_topics` is v1 bookkeeping (row counts + the
        idempotency checksum). A typed file must not appear there, or the
        per-topic row counts the UI shows would stop matching the message
        count."""
        _force_local_s3_off(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "pt.mcap"
        _write_mcap(mcap_path, [_joint_state("/joint_states", BASE_TS, [1.0, 2.0])])

        _extract_minimal_topics_to_parquet(config, "u_pt", "robot_pt", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")

        processed_topics = [
            params for sql, params in captured_sql
            if "INSERT INTO processed_topics" in sql
        ]
        assert len(processed_topics) == 1
        upload_id, topic, rows_written, _checksum, parquet_path, _attempt = (
            processed_topics[0]
        )
        assert (topic, rows_written) == ("joint_state", 1)
        assert "/v2/" not in parquet_path

    def test_stats_report_typed_rows_separately(self, tmp_path, monkeypatch):
        _force_local_mode(monkeypatch)
        config = _config(tmp_path)
        mcap_path = tmp_path / "stats.mcap"
        _write_mcap(mcap_path, [
            _joint_state("/joint_states", BASE_TS, [1.0, 2.0]),
            _tf("/tf", BASE_TS + 1_000_000, "odom", "base_link"),
        ])

        stats = {}
        rows = _extract_minimal_topics_to_parquet(
            config, "u_stats", "robot_stats", str(mcap_path), stats=stats,
            attempt_id="00000000-0000-0000-0000-0000000000a1",
        )
        assert rows == 2
        assert stats["rows_written"] == 2
        assert stats["typed_rows_written"] == {"joint_state": 2, "tf": 1}

    def test_typed_files_are_written_without_a_database(self, tmp_path, monkeypatch):
        """Same offline guard every other write has: no DATABASE_URL means
        files, no registry, no crash."""
        _force_local_mode(monkeypatch)

        def _explode(dsn):
            raise AssertionError("psycopg.connect must not be called without DATABASE_URL")

        monkeypatch.setattr(core.psycopg, "connect", _explode)

        config = _config(tmp_path)
        mcap_path = tmp_path / "offline.mcap"
        _write_mcap(mcap_path, [_joint_state("/joint_states", BASE_TS, [1.0, 2.0])])

        assert _extract_minimal_topics_to_parquet(
            config, "u_off", "robot_off", str(mcap_path),
            attempt_id="00000000-0000-0000-0000-0000000000a1",
        ) == 1
        assert _typed_files(config) == [
            "joint_state/robot_off/_attempts/00000000-0000-0000-0000-0000000000a1/joint_state/rows-u_off.parquet"
        ]


@pytest.mark.parametrize("family,schema_name,payload", [
    ("joint_state", "sensor_msgs/msg/JointState", {"name": ["joint1"], "position": [1.0]}),
    ("odometry", "nav_msgs/msg/Odometry", {"pose": {"pose": {"position": {"x": 1.0}}}}),
    ("twist", "geometry_msgs/msg/Twist", {"linear": {"x": 1.0}}),
    ("tf", "tf2_msgs/msg/TFMessage", {"transforms": [{"child_frame_id": "joint1"}]}),
    ("camera_info", "sensor_msgs/msg/CameraInfo", {"width": 640, "height": 480}),
    ("pointcloud_stats", "sensor_msgs/msg/PointCloud2", {"width": 0, "height": 1}),
])
def test_typed_sources_remain_distinguishable_in_shared_bucket(
    tmp_path, monkeypatch, family, schema_name, payload
):
    """Same-time, same-type channels must not erase their source identity."""
    _force_local_mode(monkeypatch)
    monkeypatch.setenv("SEGMENTATION_ENABLED", "false")
    config = _config(tmp_path)
    mcap_path = tmp_path / "two_sources.mcap"
    sources = ["/left/sensor", "/right/sensor"]
    _write_mcap(mcap_path, [
        (schema_name, source, "json", payload, BASE_TS) for source in sources
    ])
    _extract_minimal_topics_to_parquet(config, "u_sources", "robot", str(mcap_path), attempt_id="00000000-0000-0000-0000-0000000000a1")
    files = list((config.parquet_root / "v2" / family).rglob("*.parquet"))
    assert len(files) == 1  # Existing family/bucket discovery remains compatible.
    table = pq.read_table(files[0])
    assert table.column("raw_topic").to_pylist() == sources
    assert len(set(table.column("topic").to_pylist())) == 1
    meta = parquet_schema.read_file_metadata(table.schema)
    assert meta["schema_hash"] == parquet_schema.schema_hash(2, family)

    # Mixed historical/new files retain NULL for genuinely unknown identity.
    import duckdb
    legacy_path = tmp_path / "legacy.parquet"
    pq.write_table(table.drop(["raw_topic"]), legacy_path)
    with duckdb.connect() as con:
        rows = con.execute(
            "SELECT raw_topic FROM read_parquet(?, union_by_name=true)",
            [[str(legacy_path), str(files[0])]],
        ).fetchall()
    assert rows == [(None,), (None,), (sources[0],), (sources[1],)]
