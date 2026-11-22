"""Versioned Parquet schema for the processed lake.

Single source of truth for everything a reader needs to know about a
processed Parquet file *without opening it*:

  - the column list + Arrow types the processor writes (`arrow_schema`),
  - the schema version number, stamped into every file's key-value
    metadata and into `parquet_files.schema_version` (`SCHEMA_VERSION`),
  - a stable `schema_hash` derived from the columns/types, so a reader can
    detect "this file's shape is not the one I was compiled against" with a
    single string compare (`schema_hash`),
  - which S3 prefix a given schema version lives under (`parquet_prefix`).

Readers can tell which shape a file has from this module and the registry,
without opening the file.

Why v1 keeps the unversioned S3 prefix
--------------------------------------
`parquet_prefix()` returns the *unversioned* `processed/parquet` root for
version 1 and only inserts a `v{N}/` segment from version 2 on. That is
deliberate: the `processed/parquet/{robot}/...` layout is reconstructed by
string template in several places that are load-bearing for data safety and
for the UI --

  - the UI's Downloads listing and `tests/e2e` cleanup both build
    `processed/parquet/{robot}/` prefixes.

Existing deployments hold v1 data at the unversioned prefix. Relocating
it, or writing new *v1-shaped* files beside it under a `v1/` segment, buys
nothing (the column set is identical) and costs a coordinated change in
every one of those templates. The version discriminator only becomes
load-bearing when the columns actually change -- so it is bound to the
version number here, and the *registry* (`parquet_files`, populated by
`core.py`) is what readers resolve paths through. Bumping `SCHEMA_VERSION`
to 2 therefore moves new files to `processed/parquet/v2/...` with no change
required in the GC or the query service, both of which read the registry.

Named schema families at version 2
----------------------------------
Version 1 is one schema for every topic: `ts,topic,x,y,z`, three numeric
slots that mean something different per message type (IMU acceleration,
Twist linear velocity, Image width/height) and nothing at all for the ~60
allowlisted types with no extractor. That shape cannot express a message
with more than three numbers in it, which most robot messages need --
a JointState carries N joints x 3 quantities, an
Odometry 13 pose/twist components, a TFMessage a list of transforms.

Version 2 is therefore not one schema but a *family* of them, keyed by
name: `arrow_schema(2, family="joint_state")`. Each family is a fixed,
explicit Arrow schema (never inferred, never a function of the data -- see
`_IncrementalParquetSink`'s docstring in `core.py` for the schema-drift
trap that discipline exists to avoid), and each gets its own directory
under the version segment:

    processed/parquet/v2/{family}/{robot_id}/{topic}/rows-{upload_id}.parquet
    ^-------- parquet_prefix(root, 2, family=...) --------^

Family sits directly under `v{N}/` because it is the schema discriminator:
one directory holds exactly one column set, so a bare listing (or a
`read_parquet` over a subtree) is unambiguous without opening a file --
the same property the version segment buys, one level down.

**v1 and v2 are additive, never alternatives.** `SCHEMA_VERSION` stays 1:
the v1 files, their layout and their registry rows are untouched, and the
typed families are *extra* files written beside them for the topics that
have a typed extractor. `TYPED_SCHEMA_VERSION` names the version the
families live at, so bumping one never silently moves the other.

Source identity in v2
---------------------
New typed files append a nullable `raw_topic` string to every family. `topic`
continues to carry the normalized bucket used by existing queries. Existing
files and their registry hashes are unchanged; newly written files carry the
new column list and its corresponding hash. Readers combining generations
must union columns by name and treat an absent `raw_topic` as unknown: old
files cannot recover source identity from a normalized bucket. The v2 paths
and family names stay stable for query discovery.

Adding a family to v2 (example: `twist`)
----------------------------------------
A *new family* is an additive change to v2 in exactly the way v2 is an
additive change to v1, and it is deliberately NOT a version bump:

  * every existing family's column set -- and therefore its `schema_hash`,
    which readers compare against `parquet_files.schema_hash` -- is
    untouched, so nothing already written stops matching its registry row;
  * the new family gets its own directory (`v2/{family}/...`) and its own
    `parquet_files` rows, so no reader that does not ask for it can see it,
    and "one directory, one column set" stays true;
  * no historical file is moved or rewritten. Uploads processed before the
    family existed simply have none of its files, forever, unless they are
    reprocessed.

That last point is the contract for readers: a family's ABSENCE for a given
upload is normal and load-bearing, never an error to paper over. A consumer
must treat a missing family as normal. A consumer that silently substituted
something else would be inventing data for the exact uploads that have none.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

import pyarrow as pa

# Current schema version. Files written today carry this in their Parquet
# key-value metadata and in `parquet_files.schema_version`.
SCHEMA_VERSION = 1

# Version the named typed families live at. Deliberately a
# SEPARATE constant from SCHEMA_VERSION rather than a bump of it: the v1
# `ts,topic,x,y,z` files are still written, unchanged, for every topic --
# the families are additional files for the subset of message types that
# have a typed extractor. Bumping one constant must never move the other's
# files.
TYPED_SCHEMA_VERSION = 2

# Prefix for every key-value metadata entry we stamp into a Parquet file, so
# our keys can never collide with a writer library's own (e.g. pyarrow's
# `ARROW:schema`).
METADATA_PREFIX = "robot_vault."

# Columns the processor extracts today. `ts`/`topic` are structural (every
# reader needs them); `x`/`y`/`z` are the three numeric fields
# `extractors.extract_xyz` produces per message type. Most of the 67
# allowlisted ROS 2 types have no extractor yet and return (None, None,
# None), so their files are genuinely all-NULL in x/y/z -- which is why the
# writer records `all_null_columns` per file in the registry instead of
# letting a reader assume x/y/z always carry data.
_SCHEMAS: Dict[int, pa.Schema] = {
    1: pa.schema(
        [
            pa.field("ts", pa.timestamp("ns", tz=None)),
            pa.field("topic", pa.string()),
            pa.field("x", pa.float64()),
            pa.field("y", pa.float64()),
            pa.field("z", pa.float64()),
        ]
    ),
}

# Typed schema families. Every one is FIXED and explicit: the
# columns are a property of the family, never of the data that happens to
# arrive. `ts`/`topic` lead each of them for the same reason they lead v1
# (every reader needs them, and `STRUCTURAL_COLUMNS` below is what tells
# `value_columns` where the payload starts).
#
# Long/tidy, not wide: `joint_state` and `tf` emit ONE ROW PER JOINT and
# ONE ROW PER TRANSFORM rather than a column per joint or per frame pair.
# A wide layout would make the column set a function of the robot in the
# bag -- which is exactly the inferred-schema failure mode this module
# exists to prevent (a ParquetWriter fixes its schema on open; the first
# 6-joint arm and a later 7-joint one could not share a file, let alone a
# family), and it would need a schema migration for every new joint set.
# A long file answers "position of joint X over time" with a filter, and
# the wide view is a pivot the reader chooses, per episode, against a
# known, ordered joint list.
_FAMILY_SCHEMAS: Dict[int, Dict[str, pa.Schema]] = {
    TYPED_SCHEMA_VERSION: {
        # sensor_msgs/msg/JointState -- one row per joint per message.
        "joint_state": pa.schema(
            [
                pa.field("ts", pa.timestamp("ns", tz=None)),
                pa.field("topic", pa.string()),
                pa.field("joint_name", pa.string()),
                pa.field("position", pa.float64()),
                pa.field("velocity", pa.float64()),
                pa.field("effort", pa.float64()),
            ]
        ),
        # nav_msgs/msg/Odometry -- pose (position + orientation quaternion)
        # and twist (linear + angular velocity), one row per message.
        "odometry": pa.schema(
            [
                pa.field("ts", pa.timestamp("ns", tz=None)),
                pa.field("topic", pa.string()),
                pa.field("px", pa.float64()),
                pa.field("py", pa.float64()),
                pa.field("pz", pa.float64()),
                pa.field("qx", pa.float64()),
                pa.field("qy", pa.float64()),
                pa.field("qz", pa.float64()),
                pa.field("qw", pa.float64()),
                pa.field("vx", pa.float64()),
                pa.field("vy", pa.float64()),
                pa.field("vz", pa.float64()),
                pa.field("wx", pa.float64()),
                pa.field("wy", pa.float64()),
                pa.field("wz", pa.float64()),
            ]
        ),
        # geometry_msgs/msg/Twist -- the commanded velocity, one row per
        # message. SIX columns because a Twist carries six numbers: v1's
        # three slots hold (linear.x, linear.y, linear.z) and drop the
        # angular half entirely, so the COMMANDED yaw rate is carried by no
        # v1 file.
        #
        # `linear_*`/`angular_*`, not odometry's `vx..wz`: this whole row IS
        # a twist, so the message's own field names are unambiguous here and
        # they are the names a velocity-command consumer expects
        # (`linear_x`, `angular_z`). `odometry` spends `vx`/`wz` on telling
        # its twist half apart from its pose half inside one row; nothing
        # here needs that disambiguation.
        #
        # Every component is kept: a holonomic base commands linear.y, and a
        # message the lake did not record cannot be recovered later.
        "twist": pa.schema(
            [
                pa.field("ts", pa.timestamp("ns", tz=None)),
                pa.field("topic", pa.string()),
                pa.field("linear_x", pa.float64()),
                pa.field("linear_y", pa.float64()),
                pa.field("linear_z", pa.float64()),
                pa.field("angular_x", pa.float64()),
                pa.field("angular_y", pa.float64()),
                pa.field("angular_z", pa.float64()),
            ]
        ),
        # tf2_msgs/msg/TFMessage -- one row per transform in the message.
        # `is_static` records which topic it came from (`/tf_static` vs
        # `/tf`), because static-TF completeness is a QA concern
        # and the two topics normalize to the same `tf` bucket.
        "tf": pa.schema(
            [
                pa.field("ts", pa.timestamp("ns", tz=None)),
                pa.field("topic", pa.string()),
                pa.field("parent_frame", pa.string()),
                pa.field("child_frame", pa.string()),
                pa.field("tx", pa.float64()),
                pa.field("ty", pa.float64()),
                pa.field("tz", pa.float64()),
                pa.field("qx", pa.float64()),
                pa.field("qy", pa.float64()),
                pa.field("qz", pa.float64()),
                pa.field("qw", pa.float64()),
                pa.field("is_static", pa.bool_()),
            ]
        ),
        # sensor_msgs/msg/CameraInfo -- intrinsics pulled out of the 3x3
        # `k` matrix, one row per message.
        "camera_info": pa.schema(
            [
                pa.field("ts", pa.timestamp("ns", tz=None)),
                pa.field("topic", pa.string()),
                pa.field("width", pa.int32()),
                pa.field("height", pa.int32()),
                pa.field("fx", pa.float64()),
                pa.field("fy", pa.float64()),
                pa.field("cx", pa.float64()),
                pa.field("cy", pa.float64()),
                pa.field("distortion_model", pa.string()),
            ]
        ),
        # sensor_msgs/msg/PointCloud2 -- bounded-sample statistics, one row
        # per message. By design, LiDAR goes to
        # Parquet as downsampled stats only; no raw point-cloud media in
        # v1. `sample_n` is recorded next to `n_points` so a reader can see
        # the bounds are an estimate over a sample, not the full cloud.
        "pointcloud_stats": pa.schema(
            [
                pa.field("ts", pa.timestamp("ns", tz=None)),
                pa.field("topic", pa.string()),
                pa.field("n_points", pa.int64()),
                pa.field("sample_n", pa.int64()),
                pa.field("x_min", pa.float64()),
                pa.field("x_max", pa.float64()),
                pa.field("y_min", pa.float64()),
                pa.field("y_max", pa.float64()),
                pa.field("z_min", pa.float64()),
                pa.field("z_max", pa.float64()),
            ]
        ),
    },
}

# Every v2 family ends with the nullable source-identity column.
for _schemas_by_family in _FAMILY_SCHEMAS.values():
    for _family_name, _family_schema in _schemas_by_family.items():
        _schemas_by_family[_family_name] = _family_schema.append(
            pa.field("raw_topic", pa.string())
        )

# Columns that are structural rather than extracted data. Readers aggregate
# over the remainder (see `value_columns`).
STRUCTURAL_COLUMNS = ("ts", "topic")


def families(version: int = TYPED_SCHEMA_VERSION) -> List[str]:
    """Names of the schema families defined at `version` (empty for v1)."""
    return sorted(_FAMILY_SCHEMAS.get(version, {}))


def arrow_schema(version: int = SCHEMA_VERSION, family: Optional[str] = None) -> pa.Schema:
    """Arrow schema for `version`, or for one named family of it.

    `family=None` is the unnamed (v1) schema. Raises KeyError for an
    unknown version, or for an unknown family within a known version --
    both are programming errors, and failing loudly beats writing a file
    whose shape nothing can describe.
    """
    if family is None:
        return _SCHEMAS[version]
    return _FAMILY_SCHEMAS[version][family]


def column_names(version: int = SCHEMA_VERSION, family: Optional[str] = None) -> List[str]:
    """Ordered column names for `version` (optionally, for one family)."""
    return list(arrow_schema(version, family).names)


def value_columns(version: int = SCHEMA_VERSION, family: Optional[str] = None) -> List[str]:
    """Non-structural columns -- x/y/z for v1, the payload for a family.

    This is the list the writer tracks all-NULL-ness over (see
    `parquet_files.all_null_columns`), so it deliberately includes the
    non-numeric payload columns of a family (`joint_name`, `is_static`,
    `distortion_model`): "this file has no frame ids in it" is exactly as
    worth recording as "this file has no positions in it".
    """
    return [c for c in column_names(version, family) if c not in STRUCTURAL_COLUMNS]


def schema_hash(version: int = SCHEMA_VERSION, family: Optional[str] = None) -> str:
    """Stable 64-hex-char digest of a schema's columns and types.

    Deterministic across processes and releases (it hashes only the
    `name:type` pairs, in schema order), so a reader can compare the value
    stored in `parquet_files.schema_hash` against its own expectation
    without opening the file. Adding, removing, renaming or retyping a
    column changes the hash; nothing else does -- including the family
    name, which is NOT hashed: the hash answers "is this the shape I was
    compiled against", and two identically-shaped families are mutually
    readable. Family identity is carried separately, by the
    `robot_vault.schema_family` file metadata entry and the path.
    """
    schema = arrow_schema(version, family)
    payload = "\n".join(f"{field.name}:{field.type}" for field in schema)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_metadata(
    version: int = SCHEMA_VERSION,
    *,
    family: Optional[str] = None,
    **extra: Optional[str],
) -> Dict[bytes, bytes]:
    """Key-value metadata to stamp into a Parquet file.

    Always includes `robot_vault.schema_version` and `robot_vault.schema_hash`
    so a file is self-describing even if it is copied out of the lake and
    away from the `parquet_files` registry -- plus `robot_vault.schema_family`
    when the file belongs to a named family, since version alone no longer
    identifies a shape from v2 on. `extra` entries (robot_id, upload_id,
    topic, kind, ...) get the same `robot_vault.` prefix; None values are
    dropped rather than written as the string "None".
    """
    meta: Dict[bytes, bytes] = {
        f"{METADATA_PREFIX}schema_version".encode(): str(version).encode(),
        f"{METADATA_PREFIX}schema_hash".encode(): schema_hash(version, family).encode(),
    }
    if family is not None:
        meta[f"{METADATA_PREFIX}schema_family".encode()] = str(family).encode()
    for key, value in extra.items():
        if value is None:
            continue
        meta[f"{METADATA_PREFIX}{key}".encode()] = str(value).encode()
    return meta


def read_file_metadata(schema: pa.Schema) -> Dict[str, str]:
    """Inverse of `file_metadata`: pull our `robot_vault.*` entries back out.

    Takes the schema of an already-read Parquet file (`pq.read_schema(path)`
    or `table.schema`) and returns the entries with the prefix stripped, as
    plain strings. Foreign metadata (e.g. `ARROW:schema`) is ignored.
    """
    out: Dict[str, str] = {}
    for raw_key, raw_value in (schema.metadata or {}).items():
        key = raw_key.decode("utf-8", "replace")
        if not key.startswith(METADATA_PREFIX):
            continue
        out[key[len(METADATA_PREFIX):]] = raw_value.decode("utf-8", "replace")
    return out


def parquet_prefix(
    root: str, version: int = SCHEMA_VERSION, *, family: Optional[str] = None
) -> str:
    """S3/filesystem prefix that files of `version` are written under.

    Version 1 is the historical, unversioned root (see the module docstring
    for why moving it would be a data-loss change, not a cosmetic one);
    every later version gets an explicit `v{N}` path segment so two schemas
    never share a directory and a bare listing stays unambiguous.

    `family` appends the family segment (`{root}/v{N}/{family}`), extending
    the same rule one level down: from v2 on, "one directory, one column
    set" needs the family too, because the version alone no longer names a
    single shape. v1 has no families, so passing one is a ValueError rather
    than a silently ignored argument.
    """
    root = root.rstrip("/")
    if version <= 1:
        if family is not None:
            raise ValueError(
                f"schema version {version} has no families (got family={family!r})"
            )
        return root
    prefix = f"{root}/v{version}"
    return prefix if family is None else f"{prefix}/{family}"
