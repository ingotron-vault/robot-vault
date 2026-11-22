"""Unit tests for the versioned Parquet schema.

An unversioned schema (an inlined array literal such as
`["ts","topic","x","y","z"]`, with no indication in a written file of which
shape it has) cannot be extended. These tests pin down the three things that
prevent that:

  1. a written file is self-describing -- schema version + hash live in its
     Parquet key-value metadata;
  2. the S3/disk layout is a *function of the schema version*, and version 1
     resolves to the historical unversioned prefix (moving it would strand
     live objects outside `tools/s3_gc.py`'s reachability template, which
     would lose data);
  3. the schema hash changes if and only if the columns/types change.
"""
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from mcap.writer import Writer as McapWriter

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor import parquet_schema  # noqa: E402
from processor.core import ProcessorConfig, _extract_minimal_topics_to_parquet  # noqa: E402


def _write_json_imu_mcap(path: Path, readings, ts_values) -> None:
    """A real MCAP of JSON-encoded sensor_msgs/msg/Imu messages.

    JSON, not CDR: this fixture intentionally has no complete `ros2msg`
    definition, so a CDR payload must fail closed and make any value assertion
    meaningless. Same choice `test_processing_core.py::TestGoldenMcapParquet`
    documents; schema-bearing CDR is covered separately.
    """
    with path.open("wb") as f:
        writer = McapWriter(f)
        writer.start()
        schema_id = writer.register_schema(
            name="sensor_msgs/msg/Imu", encoding="jsonschema", data=b"{}"
        )
        channel_id = writer.register_channel(
            topic="/robot/imu", message_encoding="json", schema_id=schema_id
        )
        for reading, ts in zip(readings, ts_values):
            writer.add_message(
                channel_id=channel_id,
                log_time=ts,
                data=json.dumps(reading).encode("utf-8"),
                publish_time=ts,
            )
        writer.finish()


def _local_config(tmp_path) -> ProcessorConfig:
    return ProcessorConfig(
        parquet_root=tmp_path / "parquet",
        topics=[],
    )


def _force_local_mode(monkeypatch):
    """Local disk, no S3, no Postgres dual-write -- regardless of env."""
    for var in (
        "S3_BUCKET",
        "S3_ENDPOINT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


class TestSchemaDefinition:
    def test_v1_columns_are_the_shape_every_existing_file_has(self):
        """v1 must keep exactly the columns that are already on disk --
        changing them would silently invalidate every file in the lake."""
        assert parquet_schema.column_names(1) == ["ts", "topic", "x", "y", "z"]
        assert parquet_schema.value_columns(1) == ["x", "y", "z"]
        assert parquet_schema.SCHEMA_VERSION == 1

    def test_v1_schema_hash_is_pinned_to_its_literal_value(self):
        """The hash is written into every v1 file's metadata and into
        `parquet_files.schema_hash` for the whole existing lake. Recomputing
        it from the schema (as the other tests do) proves self-consistency,
        not stability -- only the literal catches a change that silently
        stops new files matching the millions of rows already registered.
        If this fails, the v1 schema moved and every stored hash is stale.
        """
        assert (
            parquet_schema.schema_hash(1)
            == "2df0660feee75822f4b8d00fe9692bc590909ca9dcedc10562fab3ace2d9513e"
        )

    def test_adding_v2_families_did_not_disturb_v1(self):
        """The v2 families are additive: the typed families live at their own
        version constant, so v1's default arguments still resolve to v1."""
        assert parquet_schema.SCHEMA_VERSION == 1
        assert parquet_schema.TYPED_SCHEMA_VERSION == 2
        assert parquet_schema.arrow_schema() == parquet_schema.arrow_schema(1)
        assert parquet_schema.column_names() == ["ts", "topic", "x", "y", "z"]
        assert parquet_schema.schema_hash() == parquet_schema.schema_hash(1)
        assert parquet_schema.families(1) == []

    def test_schema_hash_is_a_stable_64_hex_digest(self):
        """`parquet_files.schema_hash` is VARCHAR(64), and the value has to
        be reproducible across processes for a reader to compare against."""
        digest = parquet_schema.schema_hash(1)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)
        assert digest == parquet_schema.schema_hash(1)

    def test_schema_hash_tracks_columns_and_types(self, monkeypatch):
        """The hash exists to detect shape drift: adding a column, or
        retyping one, must change it."""
        original = parquet_schema.schema_hash(1)

        added = pa.schema(list(parquet_schema.arrow_schema(1)) + [pa.field("w", pa.float64())])
        monkeypatch.setitem(parquet_schema._SCHEMAS, 1, added)
        assert parquet_schema.schema_hash(1) != original

        retyped = pa.schema(
            [
                pa.field(f.name, pa.float32() if f.name == "x" else f.type)
                for f in parquet_schema.arrow_schema(1)
            ]
        )
        monkeypatch.setitem(parquet_schema._SCHEMAS, 1, retyped)
        assert parquet_schema.schema_hash(1) != original

    def test_metadata_roundtrips_and_drops_none(self):
        meta = parquet_schema.file_metadata(1, robot_id="r1", topic=None)
        assert meta[b"robot_vault.schema_version"] == b"1"
        assert meta[b"robot_vault.robot_id"] == b"r1"
        assert b"robot_vault.topic" not in meta, "None must be dropped, not written as 'None'"

        schema = parquet_schema.arrow_schema(1).with_metadata(meta)
        back = parquet_schema.read_file_metadata(schema)
        assert back["schema_version"] == "1"
        assert back["schema_hash"] == parquet_schema.schema_hash(1)
        assert back["robot_id"] == "r1"


class TestTypedFamilies:
    """Version 2 is a family of named, fixed schemas."""

    # Pinned literals, same reasoning as the v1 hash above: these go into
    # `parquet_files.schema_hash` for every typed file written, so a
    # column added, removed, renamed or retyped must fail HERE (loudly,
    # with a decision to make about existing files) rather than quietly
    # producing files that no longer match their registry rows.
    EXPECTED_HASHES = {
        "camera_info": "1964334b97d72c4c80e41e681714e23404cdde4b9c0a238ea00a3402a5e77065",
        "joint_state": "df7fdbea4f2f00eb71e1ede6de79e66d7a1af344739614a52c889482c1bef670",
        "odometry": "e905c891829c73cb176f59129343e25d8b3808ee560c9faf6efbc3900fe7fab1",
        "pointcloud_stats": "d6511eb49d820b1dc2bbeda0c77464e5260e15b43fdf3192d2e5f485b8403b5e",
        "tf": "bc7efe4ee6fff0109ead85a94c93d4c17cb5dac5995014c970c17c6aaf75c8b8",
        "twist": "f574db9e61a4a768810cee30d703546c3618a9c0e0fd041864a9e777e5f0a212",
    }

    def test_the_six_families_exist(self):
        assert parquet_schema.families(2) == sorted(self.EXPECTED_HASHES)

    def test_family_columns(self):
        """The columns are the contract every reader is written against."""
        assert parquet_schema.column_names(2, "joint_state") == [
            "ts", "topic", "joint_name", "position", "velocity", "effort",
            "raw_topic",
        ]
        assert parquet_schema.column_names(2, "odometry") == [
            "ts", "topic", "px", "py", "pz",
            "qx", "qy", "qz", "qw",
            "vx", "vy", "vz", "wx", "wy", "wz",
            "raw_topic",
        ]
        assert parquet_schema.column_names(2, "twist") == [
            "ts", "topic",
            "linear_x", "linear_y", "linear_z",
            "angular_x", "angular_y", "angular_z",
            "raw_topic",
        ]
        assert parquet_schema.column_names(2, "tf") == [
            "ts", "topic", "parent_frame", "child_frame",
            "tx", "ty", "tz", "qx", "qy", "qz", "qw", "is_static",
            "raw_topic",
        ]
        assert parquet_schema.column_names(2, "camera_info") == [
            "ts", "topic", "width", "height", "fx", "fy", "cx", "cy",
            "distortion_model",
            "raw_topic",
        ]
        assert parquet_schema.column_names(2, "pointcloud_stats") == [
            "ts", "topic", "n_points", "sample_n",
            "x_min", "x_max", "y_min", "y_max", "z_min", "z_max",
            "raw_topic",
        ]

    def test_twist_carries_the_angular_half_v1_structurally_cannot(self):
        """The `twist` family exists for exactly one reason: v1 has three
        numeric slots, they hold `linear.{x,y,z}`, and there is nowhere left
        to put the commanded yaw rate."""
        assert parquet_schema.value_columns(1) == ["x", "y", "z"]
        schema = parquet_schema.arrow_schema(2, "twist")
        assert "angular_z" in parquet_schema.value_columns(2, "twist")
        for column in ("linear_x", "linear_y", "linear_z",
                       "angular_x", "angular_y", "angular_z"):
            assert schema.field(column).type == pa.float64()

    def test_family_hashes_are_stable_and_distinct(self):
        for family, expected in self.EXPECTED_HASHES.items():
            assert parquet_schema.schema_hash(2, family) == expected
            assert parquet_schema.schema_hash(2, family) == parquet_schema.schema_hash(
                2, family
            ), "the digest must be reproducible within a process too"
        digests = {parquet_schema.schema_hash(2, f) for f in self.EXPECTED_HASHES}
        assert len(digests) == len(self.EXPECTED_HASHES)
        assert parquet_schema.schema_hash(1) not in digests

    def test_every_family_leads_with_the_structural_columns(self):
        """`ts`/`topic` are what every reader prunes and groups on, and
        `value_columns` (which the writer tracks all-NULL-ness over) is
        defined as "the rest"."""
        for family in parquet_schema.families(2):
            schema = parquet_schema.arrow_schema(2, family)
            assert schema.names[:2] == ["ts", "topic"]
            assert schema.field("ts").type == pa.timestamp("ns", tz=None)
            assert schema.field("topic").type == pa.string()
            assert parquet_schema.value_columns(2, family) == schema.names[2:]

    def test_non_numeric_payload_columns_count_as_value_columns(self):
        """"This file has no frame ids in it" is as worth recording in
        `all_null_columns` as "no positions"."""
        assert "joint_name" in parquet_schema.value_columns(2, "joint_state")
        assert "parent_frame" in parquet_schema.value_columns(2, "tf")
        assert "is_static" in parquet_schema.value_columns(2, "tf")
        assert "distortion_model" in parquet_schema.value_columns(2, "camera_info")

    def test_unknown_family_or_version_raises(self):
        """A typo must fail loudly rather than write a file whose shape
        nothing can describe."""
        with pytest.raises(KeyError):
            parquet_schema.arrow_schema(2, "no_such_family")
        with pytest.raises(KeyError):
            parquet_schema.arrow_schema(3, "joint_state")
        with pytest.raises(KeyError):
            # v2 has no unnamed schema: at v2 the family IS the shape.
            parquet_schema.arrow_schema(2)

    def test_family_metadata_identifies_the_shape(self):
        """From v2 on the version alone no longer names one shape, so the
        family is stamped into the file next to it."""
        meta = parquet_schema.file_metadata(2, family="tf", robot_id="r1", topic=None)
        assert meta[b"robot_vault.schema_version"] == b"2"
        assert meta[b"robot_vault.schema_family"] == b"tf"
        assert meta[b"robot_vault.schema_hash"].decode() == parquet_schema.schema_hash(2, "tf")
        assert b"robot_vault.topic" not in meta

        back = parquet_schema.read_file_metadata(
            parquet_schema.arrow_schema(2, "tf").with_metadata(meta)
        )
        assert back["schema_family"] == "tf"
        assert back["robot_id"] == "r1"

    def test_v1_metadata_has_no_family_entry(self):
        meta = parquet_schema.file_metadata(1, robot_id="r1")
        assert b"robot_vault.schema_family" not in meta
        assert meta[b"robot_vault.schema_hash"].decode() == parquet_schema.schema_hash(1)


class TestVersionedPrefix:
    def test_v1_keeps_the_historical_unversioned_prefix(self):
        """The query service's globs, the
        UI's Downloads listing and the e2e cleanup all rebuild
        `processed/parquet/{robot}/...` by string template. An object none
        of them can reconstruct is an object GC deletes, so v1 must not
        move."""
        assert (
            parquet_schema.parquet_prefix("robotics-data/processed/parquet", 1)
            == "robotics-data/processed/parquet"
        )

    def test_later_versions_get_an_explicit_path_segment(self):
        """Bumping SCHEMA_VERSION is the thing that splits the layout --
        and because GC and the query service both read the registry, that
        bump needs no change in either."""
        assert (
            parquet_schema.parquet_prefix("robotics-data/processed/parquet", 2)
            == "robotics-data/processed/parquet/v2"
        )
        assert (
            parquet_schema.parquet_prefix("robotics-data/processed/parquet/", 3)
            == "robotics-data/processed/parquet/v3"
        )

    def test_a_family_gets_its_own_segment_under_the_version(self):
        """One directory, one column set -- the same property the version
        segment buys, extended to families."""
        assert (
            parquet_schema.parquet_prefix(
                "robotics-data/processed/parquet", 2, family="joint_state"
            )
            == "robotics-data/processed/parquet/v2/joint_state"
        )

    def test_v1_has_no_families_and_says_so(self):
        """Silently ignoring the argument would put v2-shaped files at the
        v1 prefix, where the query globs assume x/y/z."""
        with pytest.raises(ValueError):
            parquet_schema.parquet_prefix(
                "robotics-data/processed/parquet", 1, family="joint_state"
            )


class TestWrittenFileCarriesItsVersion:
    READINGS = [
        {"x": 0.1, "y": 0.2, "z": 9.8},
        {"x": -1.5, "y": 2.5, "z": 9.75},
    ]
    TS_VALUES = [1_700_000_000_000_000_000 + i * 1_000_000 for i in range(2)]

    def _process(self, tmp_path, monkeypatch):
        _force_local_mode(monkeypatch)
        config = _local_config(tmp_path)
        mcap_path = tmp_path / "sample.mcap"
        _write_json_imu_mcap(mcap_path, self.READINGS, self.TS_VALUES)
        rows = _extract_minimal_topics_to_parquet(
            config, "upload_v1", "robot_v1", str(mcap_path),
            attempt_id="00000000-0000-0000-0000-0000000000a1",
        )
        assert rows == len(self.READINGS)
        return config

    def test_per_topic_files_are_self_describing(self, tmp_path, monkeypatch):
        config = self._process(tmp_path, monkeypatch)

        topic_path = (
            config.parquet_root / "robot_v1" / "_attempts" / "00000000-0000-0000-0000-0000000000a1"
            / "imu" / "2023" / "11" / "14" / "rows-upload_v1.parquet"
        )
        assert topic_path.exists()

        for path, expected_kind, expected_topic in (
            (topic_path, "topic", "imu"),
        ):
            meta = parquet_schema.read_file_metadata(pq.read_schema(path))
            assert meta["schema_version"] == str(parquet_schema.SCHEMA_VERSION)
            assert meta["schema_hash"] == parquet_schema.schema_hash()
            assert meta["kind"] == expected_kind
            assert meta["topic"] == expected_topic
            assert meta["robot_id"] == "robot_v1"
            assert meta["upload_id"] == "upload_v1"
            # The columns themselves are unchanged: v1 is the shape every
            # existing reader (and every file already in the lake) expects.
            assert pq.read_table(path).column_names == parquet_schema.column_names(1)

    def test_v1_output_paths_have_no_version_segment(self, tmp_path, monkeypatch):
        """Regression guard for the path decision: v1 files stay under the
        unversioned parquet root, below the attempt that wrote them. There is
        no combined `_all` copy."""
        config = self._process(tmp_path, monkeypatch)
        written = sorted(
            str(p.relative_to(config.parquet_root))
            for p in config.parquet_root.rglob("*.parquet")
        )
        assert written == [
            "robot_v1/_attempts/00000000-0000-0000-0000-0000000000a1/imu/2023/11/14/rows-upload_v1.parquet",
        ]
