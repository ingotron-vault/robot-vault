"""Hashed, incremental Parquet writers for processor v1 and typed families.

The processor owns paths, claims, registration and cleanup. These sinks own
only byte emission, row-group buffering, and the writer-time SHA-256 evidence.
"""
from __future__ import annotations

import hashlib
from typing import Callable, Dict, List, Optional, Tuple, Union

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from .parquet_schema import (
    SCHEMA_VERSION, TYPED_SCHEMA_VERSION, arrow_schema, value_columns,
)


class _HashingSink:
    """A write-only file object that tees every byte into a running sha256.

    Purpose: record the parquet link of the provenance chain.
    `parquet_files.sha256` has to be recorded by the *writer*, over the bytes
    the writer emitted, at the moment it emitted them. The alternative --
    close the file, read the object back out of S3, hash that -- is strictly
    weaker in the one case that matters: a truncated or corrupted write would
    be faithfully hashed and recorded as if it were correct, laundering the
    failure into a digest that verifies. Hashing the outgoing bytes turns
    that same failure into a `tools/dataset_verify.py --check-files`
    MISMATCH, which is what the digest is for.

    It sits between `pq.ParquetWriter` and the real output stream, so it
    costs one `hashlib.update()` per write call and no extra I/O, no extra
    memory beyond the 32-byte digest state, and no second pass over the data.

    `pq.ParquetWriter(where=<python file object>)` takes this path instead of
    the `where=<path>, filesystem=<fs>` one -- pyarrow then wraps this object
    rather than opening its own stream, which is why the sink owns and closes
    the underlying stream itself (see `_open_hashed_parquet_writer`). The
    Parquet footer records absolute column-chunk offsets, so `tell()` is
    load-bearing: it counts the bytes this object has passed through, which
    is exactly the file position for a freshly-created file.

    Stdlib `hashlib` only -- no new dependency, in a container image whose
    lockfiles are pinned elsewhere.
    """

    def __init__(self, stream):
        self._stream = stream
        self._digest = hashlib.sha256()
        self._pos = 0
        self.closed = False

    def write(self, data) -> int:
        # pyarrow hands over `bytes` today, but the buffer protocol is what
        # the interface promises; normalizing here means the digest and the
        # stream always see the identical byte string.
        if self.closed:
            raise ValueError("write to closed parquet hashing sink")
        block = data if isinstance(data, bytes) else bytes(data)
        written = self._stream.write(block)
        # PyArrow NativeFile returns None on a successful write while Python
        # binary files return the byte count.  Anything else, especially a
        # short write, cannot be allowed to acquire a digest for bytes that did
        # not reach the destination.  The exception prevents the registry
        # upsert, so an incomplete object is never presented as verified.
        if written is None:
            written = len(block)
        if isinstance(written, bool) or not isinstance(written, int):
            raise OSError(
                "parquet output stream returned a non-integer write result: "
                f"{written!r}"
            )
        if written != len(block):
            raise OSError(
                f"short parquet write: accepted {written} of {len(block)} bytes"
            )
        self._digest.update(block)
        self._pos += written
        return written

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        flush = getattr(self._stream, "flush", None)
        if flush is not None:
            flush()

    def close(self) -> None:
        """Idempotent: the cleanup path in
        `_extract_minimal_topics_to_parquet` may close a sink that already
        closed itself."""
        if self.closed:
            return
        self.closed = True
        self._stream.close()

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def hexdigest(self) -> str:
        """Lowercase hex sha256 of the finalized Parquet byte stream.

        The footer is the last thing written, and a file without it is not a
        file.  Refusing to expose an in-progress digest makes that invariant
        executable rather than relying on every future call site to remember
        it before writing `parquet_files.sha256`.
        """
        if not self.closed:
            raise RuntimeError("parquet sha256 requested before output was finalized")
        return self._digest.hexdigest()


def _open_hashed_parquet_writer(
    path: str,
    schema,
    *,
    filesystem: Optional[pafs.S3FileSystem],
    sink_factory: Callable[..., _HashingSink],
) -> Tuple[pq.ParquetWriter, _HashingSink]:
    """Open a `pq.ParquetWriter` whose bytes are teed through a sha256.

    Replaces `pq.ParquetWriter(path, schema, filesystem=fs)`, which opens its
    own stream internally and gives no way to see the bytes. `compression=None`
    on `open_output_stream` mirrors what pyarrow passes on that internal path
    (ARROW-10480: a `.parquet.gz`-looking name must not implicitly gzip), so
    the bytes on disk/in S3 are the same ones pyarrow's own path produces.
    """
    stream = (
        filesystem.open_output_stream(path, compression=None)
        if filesystem is not None
        else open(path, "wb")
    )
    sink = sink_factory(stream)
    try:
        writer = pq.ParquetWriter(sink, schema, compression="snappy")
    except Exception:
        sink.close()
        raise
    return writer, sink


class _IncrementalParquetSink:
    """Buffers rows for ONE output Parquet file and flushes them as a row
    group every `PARQUET_FLUSH_ROWS` rows, instead of holding the whole
    file's rows in Python lists until the source MCAP has been fully read.
    `_extract_minimal_topics_to_parquet` opens one of these per topic.

    THE SCHEMA-DRIFT TRAP this design exists to avoid: a
    `pyarrow.parquet.ParquetWriter` fixes a file's schema from whatever it
    is opened with, and every subsequent `write_table()` call must match
    it. If a batch's Arrow arrays were built by *inferring* types from
    Python values (e.g. `pa.Table.from_pydict`), an early batch that is
    all-None in x/y/z -- e.g. every CDR decode attempted so far failed --
    infers those columns as `null`-typed, and a later batch with real
    floats then fails `write_table` outright (verified: "Table schema does
    not match schema used to create file"). This sink never infers: every
    batch is built with `pa.array(col, type=<fixed type>)` against the
    SAME explicit `arrow_schema(SCHEMA_VERSION)` the writer itself is
    opened with (see `_flush`/`__init__`), so an all-None batch is exactly
    as valid as a fully-populated one for the same open writer -- the type
    is never a function of what happens to be in the buffer.

    Also tracks the running aggregates (row count, ts min/max, first/last ts,
    x/y/z sums, which value columns are entirely NULL) -- the full column lists no
    longer exist in full once older rows have been flushed and dropped, so
    the aggregates are updated one row at a time as `add()` is called
    instead.
    """

    def __init__(
        self,
        path: str,
        *,
        filesystem: Optional[pafs.S3FileSystem],
        metadata: Dict[bytes, bytes],
        flush_rows: int,
        writer_factory: Callable[..., Tuple[pq.ParquetWriter, _HashingSink]],
    ):
        self.path = path
        self._filesystem = filesystem
        self._flush_rows = flush_rows
        self._writer_factory = writer_factory
        # Metadata lives on the schema the WRITER is opened with -- once
        # opened that is the file's schema (and its key-value metadata),
        # independent of whether individual write_table() calls repeat it
        # (verified against both local disk and pyarrow's S3FileSystem).
        self._schema = arrow_schema(SCHEMA_VERSION).with_metadata(metadata)
        self._writer: Optional[pq.ParquetWriter] = None
        #: The sha256 tee the writer emits through. Created
        #: with the writer, read back after `close()` as `self.sha256`.
        self._sink: Optional[_HashingSink] = None
        self._buf_ts: List[int] = []
        self._buf_topic: List[str] = []
        self._buf_x: List[Optional[float]] = []
        self._buf_y: List[Optional[float]] = []
        self._buf_z: List[Optional[float]] = []

        self.row_count = 0
        self.ts_min: Optional[int] = None
        self.ts_max: Optional[int] = None
        self.ts_first: Optional[int] = None  # insertion-order first, for the checksum
        self.ts_last: Optional[int] = None   # insertion-order last, for the checksum
        self.sum_x = 0.0
        self.sum_y = 0.0
        self.sum_z = 0.0
        self._any_non_null = {"x": False, "y": False, "z": False}

    def add(self, ts_ns: int, topic: str, x: Optional[float], y: Optional[float], z: Optional[float]) -> None:
        self._buf_ts.append(ts_ns)
        self._buf_topic.append(topic)
        self._buf_x.append(x)
        self._buf_y.append(y)
        self._buf_z.append(z)

        self.row_count += 1
        self.ts_min = ts_ns if self.ts_min is None else min(self.ts_min, ts_ns)
        self.ts_max = ts_ns if self.ts_max is None else max(self.ts_max, ts_ns)
        if self.ts_first is None:
            self.ts_first = ts_ns
        self.ts_last = ts_ns
        # Summed in arrival order, filtering None exactly as
        # `sum(v for v in arr if v is not None)` over the full column does
        # -- so this reproduces the identical float64 running sum, not
        # just an equal-in-theory one.
        if x is not None:
            self._any_non_null["x"] = True
            self.sum_x += x
        if y is not None:
            self._any_non_null["y"] = True
            self.sum_y += y
        if z is not None:
            self._any_non_null["z"] = True
            self.sum_z += z

        if len(self._buf_ts) >= self._flush_rows:
            self._flush()

    def _flush(self) -> None:
        if not self._buf_ts:
            return
        batch = pa.Table.from_arrays(
            [
                pa.array(self._buf_ts, type=pa.timestamp("ns", tz=None)),
                pa.array(self._buf_topic, type=pa.string()),
                pa.array(self._buf_x, type=pa.float64()),
                pa.array(self._buf_y, type=pa.float64()),
                pa.array(self._buf_z, type=pa.float64()),
            ],
            schema=arrow_schema(SCHEMA_VERSION),
        )
        if self._writer is None:
            self._writer, self._sink = self._writer_factory(
                self.path, self._schema, filesystem=self._filesystem,
            )
        self._writer.write_table(batch)
        self._buf_ts = []
        self._buf_topic = []
        self._buf_x = []
        self._buf_y = []
        self._buf_z = []

    def close(self) -> None:
        """Flush any remainder below the threshold and finalize the file.

        Callers only construct a sink once its first row exists (see
        `_extract_minimal_topics_to_parquet`), so `_writer is None` here
        only if the sole flush point is this final one -- e.g. an upload
        with 3 rows never reaches PARQUET_FLUSH_ROWS, so the entire file
        is written as a single row group right here, same as
        `pq.write_table` would do for the whole thing.
        """
        self._flush()
        if self._writer is None:
            self._writer, self._sink = self._writer_factory(
                self.path, self._schema, filesystem=self._filesystem,
            )
        # Writer first (it emits the footer), THEN the tee -- so the digest
        # covers the complete file, and the underlying stream is closed
        # exactly once. pyarrow does not close a sink it did not open.
        try:
            self._writer.close()
        finally:
            # `finally`, so a writer that fails while emitting the footer
            # still releases the underlying handle (and, on S3, completes or
            # aborts its multipart upload) instead of leaking it. The digest
            # of a file whose footer never landed is meaningless, but the
            # caller never reaches the registry write on that path anyway --
            # the exception propagates and run_once retries the upload.
            if self._sink is not None:
                self._sink.close()

    @property
    def sha256(self) -> Optional[str]:
        """sha256 of the bytes written to this file, or None before `close()`.

        `parquet_files.sha256`: recorded by the writer, over
        what the writer wrote. None can only be seen by a caller that asks
        before closing -- every registry call site closes first.
        """
        return (
            self._sink.hexdigest()
            if self._sink is not None and self._sink.closed
            else None
        )

    @property
    def all_null_columns(self) -> List[str]:
        """Value columns (x/y/z) that were None in every row seen so far.

        Equivalent to scanning a
        fully-materialized column list after the fact -- this tracks the
        same fact (`_any_non_null`) incrementally instead, since the full
        list no longer exists once earlier rows have been flushed out.
        """
        return [c for c in value_columns(SCHEMA_VERSION) if not self._any_non_null.get(c, False)]


class _TypedParquetSink:
    """`_IncrementalParquetSink` for a v2 typed family.

    Same contract, same discipline, one difference: the columns come from
    `arrow_schema(version, family)` instead of being the hardcoded
    ts/topic/x/y/z five, so one class serves every family. Rows arrive as
    `{column: value}` dicts from `typed_extractors`, buffered per column
    and flushed as a Parquet row group every `PARQUET_FLUSH_ROWS` rows --
    so peak memory stays bounded by the flush threshold rather than by how
    many joints x how many messages an upload contains (a 12-joint arm at
    100 Hz produces 1,200 typed rows per second of bag).

    THE SCHEMA-DRIFT TRAP applies here identically and is avoided the same
    way: every batch is built with `pa.array(col, type=<the field's fixed
    type>)` against the very schema the writer was opened with, so a
    flushed row group of all-NULL positions cannot retype a column out
    from under a later one that has real floats. Nothing about a family's
    column set is ever a function of the data -- that is why the schema is
    long/tidy and fixed, not a wide schema per joint set.

    Deliberately a sibling of `_IncrementalParquetSink` rather than a
    refactor of it: the v1 path must stay byte-for-byte as it is,
    and v1 carries state this does not (the running float64 x/y/z sums
    that feed the `processed_topics` checksum, which must keep reproducing
    the exact same digest).
    """

    def __init__(
        self,
        path: str,
        *,
        family: str,
        filesystem: Optional[pafs.S3FileSystem],
        metadata: Dict[bytes, bytes],
        version: int = TYPED_SCHEMA_VERSION,
        flush_rows: int,
        writer_factory: Callable[..., Tuple[pq.ParquetWriter, _HashingSink]],
    ):
        self.path = path
        self.family = family
        self.version = version
        self._filesystem = filesystem
        self._flush_rows = flush_rows
        self._writer_factory = writer_factory
        self._base_schema = arrow_schema(version, family)
        self._schema = self._base_schema.with_metadata(metadata)
        self._writer: Optional[pq.ParquetWriter] = None
        #: Same sha256 tee the v1 sink uses -- a typed file is a Parquet
        #: object in the lake like any other and gets the same evidence.
        self._sink: Optional[_HashingSink] = None
        self._columns: List[str] = list(self._base_schema.names)
        self._buffers: Dict[str, List[object]] = {c: [] for c in self._columns}

        self.row_count = 0
        self.ts_min: Optional[int] = None
        self.ts_max: Optional[int] = None
        self._any_non_null: Dict[str, bool] = {
            c: False for c in value_columns(version, family)
        }

    def add(self, ts_ns: int, topic: str, values: Dict[str, object]) -> None:
        """Append one row. `values` supplies the payload columns; `ts` and
        `topic` are owned by the caller, exactly as in v1. A payload column
        the message did not populate is simply absent and lands as NULL."""
        self._buffers["ts"].append(ts_ns)
        self._buffers["topic"].append(topic)
        for column in self._columns:
            if column in ("ts", "topic"):
                continue
            value = values.get(column)
            self._buffers[column].append(value)
            if value is not None:
                self._any_non_null[column] = True

        self.row_count += 1
        self.ts_min = ts_ns if self.ts_min is None else min(self.ts_min, ts_ns)
        self.ts_max = ts_ns if self.ts_max is None else max(self.ts_max, ts_ns)

        if len(self._buffers["ts"]) >= self._flush_rows:
            self._flush()

    def _flush(self) -> None:
        if not self._buffers["ts"]:
            return
        batch = pa.Table.from_arrays(
            [
                pa.array(self._buffers[field.name], type=field.type)
                for field in self._base_schema
            ],
            schema=self._base_schema,
        )
        if self._writer is None:
            self._writer, self._sink = self._writer_factory(
                self.path, self._schema, filesystem=self._filesystem,
            )
        self._writer.write_table(batch)
        self._buffers = {c: [] for c in self._columns}

    def close(self) -> None:
        """Flush the remainder and finalize the file -- see
        `_IncrementalParquetSink.close` for why the writer is opened here
        when no flush ever happened, and why the tee is closed after it."""
        self._flush()
        if self._writer is None:
            self._writer, self._sink = self._writer_factory(
                self.path, self._schema, filesystem=self._filesystem,
            )
        try:
            self._writer.close()
        finally:
            # Same `finally` as the v1 sink -- see its `close()` for why.
            if self._sink is not None:
                self._sink.close()

    @property
    def sha256(self) -> Optional[str]:
        """sha256 of the bytes written to this file, or None before `close()`."""
        return (
            self._sink.hexdigest()
            if self._sink is not None and self._sink.closed
            else None
        )

    @property
    def all_null_columns(self) -> List[str]:
        """Payload columns that were NULL in every row seen so far."""
        return [
            c for c in value_columns(self.version, self.family)
            if not self._any_non_null.get(c, False)
        ]


def _required_time_bounds(
    sink: Union[_IncrementalParquetSink, _TypedParquetSink],
) -> Tuple[int, int]:
    """Return the non-empty sink bounds required by the registry schema."""
    if sink.row_count <= 0 or sink.ts_min is None or sink.ts_max is None:
        raise RuntimeError("cannot register a Parquet sink without timestamped rows")
    return sink.ts_min, sink.ts_max


def _required_sha256(
    sink: Union[_IncrementalParquetSink, _TypedParquetSink],
) -> str:
    """Return the closed sink's content digest, which the registry requires."""
    digest = sink.sha256
    if digest is None:
        raise RuntimeError("cannot register a Parquet sink before it is closed")
    return digest
