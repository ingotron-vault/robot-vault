"""The processor must STREAM an MCAP out of S3, never buffer it.

`_extract_minimal_topics_to_parquet` must not do `data = f.read()` into a
`BytesIO`: peak RSS would track object size byte for byte and one 2 GB bag could
OOM-kill the processor container. These tests fail on that behaviour.

The fake filesystem here is deliberately a real file served through a
counting proxy rather than a `Mock`: the property under test is *how much
gets read at once*, and a Mock cannot express that. It also has to behave
like the real thing in one non-obvious way — see `_CountingFile`.
"""
import io
import json
import os
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import pytest
from mcap.writer import Writer as McapWriter

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor.core import (  # noqa: E402
    MCAP_READ_BUFFER_BYTES,
    ProcessorConfig,
    _extract_minimal_topics_to_parquet,
)

BASE_TS = 1_700_000_000_000_000_000
PAYLOAD_LEN = 8 * 1024


class _CountingFile:
    """Proxies an open file, recording how much is read in a single call.

    NOT an `io.RawIOBase`. mcap's `StreamReader.__init__` special-cases
    `RawIOBase` by wrapping it in a `BufferedReader` it owns, and that
    wrapper's destructor closes the underlying stream — which surfaces
    mid-iteration as "I/O operation on closed file", the symptom a
    read-everything workaround would hide. A PyArrow
    `NativeFile` (what production passes) is not a `RawIOBase` either, so
    this proxy models production only by staying off that branch.
    """

    closed = False

    def __init__(self, inner):
        self._inner = inner
        self.reads = 0
        self.bytes_read = 0
        self.max_single_read = 0

    # --- the file protocol mcap and io.BufferedReader actually use ---
    def readable(self):
        return True

    def seekable(self):
        return True

    def writable(self):
        return False

    def _account(self, data):
        self.reads += 1
        self.bytes_read += len(data)
        self.max_single_read = max(self.max_single_read, len(data))
        return data

    def read(self, size=-1):
        if size is None or size < 0:
            return self._account(self._inner.read())
        return self._account(self._inner.read(size))

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def seek(self, offset, whence=io.SEEK_SET):
        return self._inner.seek(offset, whence)

    def tell(self):
        return self._inner.tell()

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeS3FileSystem:
    """Serves one local path for any key, and hands back a counting file."""

    def __init__(self, backing_path: Path):
        self._backing = backing_path
        self.last_file: _CountingFile | None = None
        self.opened_paths: list[str] = []

    def open_input_file(self, path):
        self.opened_paths.append(path)
        handle = self._backing.open("rb")
        self.last_file = _CountingFile(handle)
        return _ClosingContext(self.last_file, handle)


class _ClosingContext:
    """`open_input_file` is used as a context manager in production."""

    def __init__(self, counting, raw):
        self._counting = counting
        self._raw = raw

    def __enter__(self):
        return self._counting

    def __exit__(self, *exc):
        self._raw.close()
        return False

    # PyArrow's NativeFile is usable without `with` too.
    def __getattr__(self, name):
        return getattr(self._counting, name)


def _write_mcap(
    path: Path,
    message_count: int,
    schema_name: str,
    payload_len: int = PAYLOAD_LEN,
) -> None:
    """An MCAP with incompressible payloads.

    JSON IMU messages carry real acceleration values and a random frame_id.
    The frame_id varies payload size without changing the extracted row count.
    Identical padding compresses to almost nothing inside a chunk, which would
    leave a tiny file and make the streaming memory measurement meaningless.
    """
    pool = os.urandom(max(4 * 1024 * 1024, payload_len * 2))
    with path.open("wb") as f:
        w = McapWriter(f)
        w.start()
        schema_id = w.register_schema(
            name=schema_name, encoding="jsonschema", data=b"{}"
        )
        channel_id = w.register_channel(
            topic="/imu/data", message_encoding="json", schema_id=schema_id
        )
        for i in range(message_count):
            off = (i * payload_len) % (len(pool) - payload_len)
            w.add_message(
                channel_id=channel_id,
                log_time=BASE_TS + i * 1_000_000,
                data=json.dumps({
                    "header": {"frame_id": pool[off:off + payload_len].hex()},
                    "linear_acceleration": {"x": 1.0, "y": 2.0, "z": 9.8},
                }).encode("utf-8"),
                publish_time=BASE_TS + i * 1_000_000,
            )
        w.finish()


@pytest.fixture
def big_mcap(tmp_path):
    """~16 MiB: many chunks (mcap chunks at ~1 MiB), so a whole-object read
    is unmistakable next to a windowed one, while staying fast enough for
    the default unit run."""
    path = tmp_path / "big.mcap"
    _write_mcap(path, message_count=2048, schema_name="sensor_msgs/msg/Imu")
    assert path.stat().st_size > 8 * 1024 * 1024, "fixture must be big enough to matter"
    return path


def _config(tmp_path) -> ProcessorConfig:
    return ProcessorConfig(
        parquet_root=tmp_path / "parquet",
        topics=[],
    )


def test_s3_object_is_never_read_whole(big_mcap, tmp_path, monkeypatch):
    """The regression guard. `f.read()` with no size returns the entire
    object in one call; streaming cannot exceed its read-ahead window."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_fs = _FakeS3FileSystem(big_mcap)
    file_size = big_mcap.stat().st_size

    with patch("processor.core._s3_filesystem", return_value=fake_fs):
        rows = _extract_minimal_topics_to_parquet(
            _config(tmp_path), "upload-stream", "robot1", "s3://bucket/raw/big.mcap",
            attempt_id="00000000-0000-0000-0000-0000000000a1",
        )

    counted = fake_fs.last_file
    assert counted is not None, "the S3 branch never opened the object"

    # The property under test: no single read materialises the object.
    assert counted.max_single_read <= MCAP_READ_BUFFER_BYTES, (
        f"a single read pulled {counted.max_single_read} bytes of a "
        f"{file_size}-byte object; the read-ahead window is "
        f"{MCAP_READ_BUFFER_BYTES}"
    )
    # ...and it took more than one read to get through the file, i.e. the
    # reader really did walk it rather than slurping and seeking in memory.
    assert counted.reads > 1
    # The data still arrives: 2048 IMU messages, all decoded to rows.
    assert rows == 2048


def _peak_allocation_processing(path: Path, tmp_path: Path) -> int:
    fake_fs = _FakeS3FileSystem(path)
    tracemalloc.start()
    try:
        with patch("processor.core._s3_filesystem", return_value=fake_fs):
            _extract_minimal_topics_to_parquet(
                _config(tmp_path), f"upload-{path.stem}", "robot1",
                f"s3://bucket/raw/{path.name}",
                attempt_id="00000000-0000-0000-0000-0000000000a1",
            )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_s3_streaming_peak_memory_does_not_scale_with_object_size(
    big_mcap, tmp_path, monkeypatch
):
    """Peak allocation of the READ is O(window + chunk), not O(object).

    A fixed ceiling would only be a proxy; the property that keeps a 2 GB
    bag from OOM-killing the processor is that peak memory stops tracking
    object size. So process two files whose sizes differ by 8x and require
    peak to grow at a small fraction of that rate.

    The two fixtures carry the SAME NUMBER OF MESSAGES and differ only in
    payload size, which matters and is not incidental.
    The processor buffers extracted rows until a Parquet row-group flush.
    Holding the row count fixed keeps those buffers identical and isolates
    the read path from row-buffer allocation.

    Measured: read path 0.0024 streaming, versus ~1.0 for a
    read-into-BytesIO path, whose peak tracks object size byte for byte.

    `tracemalloc` rather than `ru_maxrss`: the latter is a whole-process
    high-water mark that never falls, so an earlier test in the same run
    would mask a regression here.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    small_mcap = tmp_path / "small_payload.mcap"
    big_payload_mcap = tmp_path / "big_payload.mcap"
    _write_mcap(small_mcap, message_count=512, schema_name="sensor_msgs/msg/Imu")
    _write_mcap(
        big_payload_mcap, message_count=512, schema_name="sensor_msgs/msg/Imu",
        payload_len=8 * PAYLOAD_LEN,
    )

    small_size = small_mcap.stat().st_size
    big_size = big_payload_mcap.stat().st_size
    assert big_size > small_size * 3, "the two fixtures must differ enough to be a slope"

    peak_small = _peak_allocation_processing(small_mcap, tmp_path)
    peak_big = _peak_allocation_processing(big_payload_mcap, tmp_path)

    growth_rate = (peak_big - peak_small) / (big_size - small_size)
    assert growth_rate < 0.1, (
        f"peak allocation grew {peak_big - peak_small} bytes across a "
        f"{big_size - small_size}-byte increase in object size "
        f"(rate {growth_rate:.4f}); streaming measures ~0.002, and "
        f"reading the object whole measures ~1.0"
    )
    # Belt and braces: never hold the whole object at once either.
    assert peak_big < big_size, (
        f"peak allocation {peak_big} exceeds the {big_size}-byte object"
    )


def test_local_path_still_streams(big_mcap, tmp_path, monkeypatch):
    """The local-disk branch streams; keep it that way so the
    two branches cannot drift apart."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for var in ("S3_BUCKET", "S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)

    rows = _extract_minimal_topics_to_parquet(
        _config(tmp_path), "upload-local", "robot1", str(big_mcap),
        attempt_id="00000000-0000-0000-0000-0000000000a1",
    )
    assert rows == 2048
