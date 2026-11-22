"""Bounded MCAP reading: the processor must not run out of memory on hostile input.

Written before ``processor/mcap_bounds.py``. Ways the bounded reader can fail:

 1. A Chunk record declares an ``uncompressed_size`` above the cap, and the
    reader decompresses it anyway.
 2. A Chunk record declares a small ``uncompressed_size`` but its zstd frame
    header declares a huge content size. ``zstandard.decompress`` trusts the
    frame header, so the reader allocates the frame's size, not the record's.
 3. The same lie inside an lz4 frame.
 4. The summary ChunkIndex states small sizes, the real Chunk record is large,
    and the reader checks only the index.
 5. A top-level record states a huge length, and the reader reads the body
    before it checks the length.
 6. A single message is larger than the message cap.
 7. The file declares more channels or schemas than the cap.
 8. A Message record is shorter than its 22-byte header. The mcap library then
    calls ``read(negative)``, which reads to the end of the file.
 9. Chunk indexes lie about start times, so log-time ordering decompresses
    every chunk before it yields one message.
10. Many tiny messages: the data bytes are small, but the Python objects are not.
11. A file with no summary section. The mcap library then sorts every message
    of the file in memory.
12. A chunk index points outside the data section, or at a record that is not
    a Chunk.
13. Duplicate chunk indexes yield the same messages twice.
14. Valid files give different messages or a different order than the mcap
    library.
15. A truncated file (no footer) is accepted silently.
16. An unknown compression name is accepted.
17. A large attachment, which the processor never reads, is rejected.
18. A limit violation is retried, so one upload is attempted again and again.
19. A limit violation stops the processing of other uploads.
20. Invalid limit settings are accepted, or are reported only after an upload
    was claimed.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path
from unittest.mock import Mock

import lz4.frame
import pytest
import zstandard
from mcap.data_stream import RecordBuilder
from mcap.exceptions import McapError
from mcap.reader import make_reader as library_make_reader
from mcap.records import (
    Attachment,
    Channel,
    Chunk,
    ChunkIndex,
    DataEnd,
    Footer,
    Header,
    Message,
    Schema,
)
from mcap.writer import CompressionType, Writer

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor import core  # noqa: E402
from processor.mcap_bounds import (  # noqa: E402
    McapLimitError,
    McapLimits,
    TerminalInputError,
    make_reader,
)

MAGIC = b"\x89MCAP0\r\n"
KIB = 1024
MIB = 1024 * 1024


def _limits(**overrides) -> McapLimits:
    values = dict(
        max_chunk_bytes=1 * MIB,
        max_message_bytes=256 * KIB,
        max_channels=8,
        max_schemas=8,
        max_buffered_bytes=4 * MIB,
    )
    values.update(overrides)
    return McapLimits(**values)


def _read_all(data: bytes, limits: McapLimits):
    return [
        (channel.topic, message.log_time, message.sequence, message.data)
        for _schema, channel, message in make_reader(
            io.BytesIO(data), limits=limits
        ).iter_messages()
    ]


def _library_read_all(data: bytes):
    return [
        (channel.topic, message.log_time, message.sequence, message.data)
        for _schema, channel, message in library_make_reader(
            io.BytesIO(data)
        ).iter_messages()
    ]


def _writer_file(*, compression, use_chunking=True, chunk_size=512, count=60):
    out = io.BytesIO()
    writer = Writer(
        out,
        chunk_size=chunk_size,
        compression=compression,
        use_chunking=use_chunking,
    )
    writer.start()
    schema_id = writer.register_schema(
        name="std_msgs/msg/String", encoding="ros2msg", data=b"string data"
    )
    channels = [
        writer.register_channel(
            topic=f"/topic_{index}", message_encoding="cdr", schema_id=schema_id
        )
        for index in range(3)
    ]
    for index in range(count):
        writer.add_message(
            channel_id=channels[index % 3],
            log_time=1_000 + index * 10,
            publish_time=1_000 + index * 10,
            sequence=index,
            data=bytes([index % 251]) * (16 + index),
        )
    writer.finish()
    return out.getvalue()


def _records(*records) -> bytes:
    builder = RecordBuilder()
    for record in records:
        record.write(builder)
    return builder.end()


def _schema(schema_id=1):
    return Schema(id=schema_id, name="std_msgs/msg/String", encoding="ros2msg", data=b"string data")


def _channel(channel_id=1, schema_id=1, topic="/t"):
    return Channel(
        id=channel_id,
        topic=topic,
        message_encoding="cdr",
        schema_id=schema_id,
        metadata={},
    )


def _message(channel_id=1, log_time=1, data=b"x", sequence=0):
    return Message(
        channel_id=channel_id,
        log_time=log_time,
        publish_time=log_time,
        sequence=sequence,
        data=data,
    )


def _compress(compression: str, raw: bytes) -> bytes:
    if compression == "zstd":
        return zstandard.ZstdCompressor().compress(raw)
    if compression == "lz4":
        return lz4.frame.compress(raw)
    return raw


def _crafted(
    chunks,
    *,
    summary=True,
    top_level=(),
    summary_definitions=None,
    index_overrides=None,
    extra_index_offsets=(),
):
    """Build an MCAP byte string by hand so that sizes and indexes can lie.

    ``chunks`` is a list of dicts: ``raw`` (inner record bytes), optional
    ``compression``, ``declared`` (uncompressed_size to write), ``start``.
    """

    builder = RecordBuilder()
    Header(profile="ros2", library="test").write(builder)
    for record in top_level:
        record.write(builder)
    indexes = []
    for position, spec in enumerate(chunks):
        compression = spec.get("compression", "zstd")
        raw = spec["raw"]
        payload = spec.get("payload", _compress(compression, raw))
        offset = len(MAGIC) + builder.count
        chunk = Chunk(
            compression=compression,
            data=payload,
            message_start_time=spec.get("start", 0),
            message_end_time=spec.get("end", 0),
            uncompressed_crc=0,
            uncompressed_size=spec.get("declared", len(raw)),
        )
        chunk.write(builder)
        length = len(MAGIC) + builder.count - offset
        index = dict(
            chunk_length=length,
            chunk_start_offset=offset,
            compression=compression,
            compressed_size=len(payload),
            message_end_time=spec.get("end", 0),
            message_index_length=0,
            message_index_offsets={},
            message_start_time=spec.get("start", 0),
            uncompressed_size=spec.get("declared", len(raw)),
        )
        index.update((index_overrides or {}).get(position, {}))
        indexes.append(ChunkIndex(**index))
    for offset in extra_index_offsets:
        indexes.append(
            ChunkIndex(
                chunk_length=1,
                chunk_start_offset=offset,
                compression="zstd",
                compressed_size=1,
                message_end_time=0,
                message_index_length=0,
                message_index_offsets={},
                message_start_time=0,
                uncompressed_size=1,
            )
        )
    DataEnd(data_section_crc=0).write(builder)
    summary_start = 0
    if summary:
        summary_start = len(MAGIC) + builder.count
        definitions = (
            summary_definitions
            if summary_definitions is not None
            else [_schema(), _channel()]
        )
        for record in definitions:
            record.write(builder)
        for index in indexes:
            index.write(builder)
    Footer(summary_start=summary_start, summary_offset_start=0, summary_crc=0).write(builder)
    return MAGIC + builder.end() + MAGIC


def _inner(*messages, definitions=True) -> bytes:
    records = [_schema(), _channel()] if definitions else []
    return _records(*records, *messages)


# ---------------------------------------------------------------------------
# 14, 13: valid files and duplicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "compression",
    [CompressionType.ZSTD, CompressionType.LZ4, CompressionType.NONE],
)
def test_valid_chunked_file_matches_library_messages_and_order(compression):
    data = _writer_file(compression=compression)
    expected = _library_read_all(data)
    assert len(expected) == 60
    assert _read_all(data, _limits()) == expected


def test_valid_unchunked_file_matches_library():
    data = _writer_file(compression=CompressionType.NONE, use_chunking=False)
    assert _read_all(data, _limits()) == _library_read_all(data)


def test_schema_and_channel_objects_are_returned():
    data = _writer_file(compression=CompressionType.ZSTD, count=3)
    schema, channel, message = next(
        make_reader(io.BytesIO(data), limits=_limits()).iter_messages()
    )
    assert schema.name == "std_msgs/msg/String"
    assert schema.data == b"string data"
    assert channel.message_encoding == "cdr"
    assert message.data == bytes([0]) * 16


def test_overlapping_chunks_are_merged_in_log_time_order():
    first = _inner(_message(log_time=10), _message(log_time=30, sequence=2))
    second = _inner(_message(log_time=20, sequence=1), definitions=False)
    data = _crafted(
        [
            {"raw": first, "start": 10, "end": 30},
            {"raw": second, "start": 20, "end": 20},
        ]
    )
    assert [row[1] for row in _read_all(data, _limits())] == [10, 20, 30]
    assert _read_all(data, _limits()) == _library_read_all(data)


def test_duplicate_chunk_indexes_yield_messages_once():
    raw = _inner(_message(log_time=5))
    plain = _crafted([{"raw": raw, "start": 5}])
    first_chunk_offset = len(MAGIC) + len(_records(Header(profile="ros2", library="test")))
    data = _crafted(
        [{"raw": raw, "start": 5}], extra_index_offsets=[first_chunk_offset]
    )
    assert len(_read_all(plain, _limits())) == 1
    assert len(_read_all(data, _limits())) == 1


# ---------------------------------------------------------------------------
# 1-4: chunk size caps use the real Chunk record and the real decompressed size
# ---------------------------------------------------------------------------


def test_declared_uncompressed_size_above_cap_is_rejected_before_decompression():
    # The payload is not valid zstd: a reader that decompressed first would
    # raise a zstd error instead of the limit error.
    data = _crafted(
        [{"raw": b"", "payload": b"not zstd", "declared": 2 * MIB}]
    )
    with pytest.raises(McapLimitError, match="uncompressed size"):
        _read_all(data, _limits())


@pytest.mark.parametrize("compression", ["zstd", "lz4"])
def test_frame_larger_than_declared_size_is_rejected(compression):
    raw = _inner(_message(data=b"\0" * (200 * KIB)))
    big = raw * 20  # about 4 MiB of records, far above the 1 MiB cap
    data = _crafted(
        [{"raw": big, "compression": compression, "declared": 4 * KIB}]
    )
    with pytest.raises(McapLimitError, match="more than its declared"):
        _read_all(data, _limits())


def test_lying_chunk_index_does_not_hide_large_chunk_record():
    raw = _inner(_message(data=b"\0" * (100 * KIB)))
    data = _crafted(
        [{"raw": raw, "declared": 2 * MIB}],
        index_overrides={0: {"uncompressed_size": 10, "compressed_size": 10}},
    )
    with pytest.raises(McapLimitError, match="uncompressed size"):
        _read_all(data, _limits())


def test_compressed_chunk_record_above_cap_is_rejected_before_read():
    raw = _inner(_message(data=b"\0" * 10))
    data = _crafted(
        [{"raw": raw, "compression": "", "payload": b"\0" * (2 * MIB), "declared": 10}]
    )
    with pytest.raises(McapLimitError, match="record"):
        _read_all(data, _limits())


# ---------------------------------------------------------------------------
# 5, 6, 8: record and message sizes
# ---------------------------------------------------------------------------


def test_message_above_cap_inside_chunk_is_rejected():
    raw = _inner(_message(data=b"\1" * (300 * KIB)))
    data = _crafted([{"raw": raw}])
    with pytest.raises(McapLimitError, match="message"):
        _read_all(data, _limits())


def test_top_level_message_above_cap_is_rejected_without_summary():
    data = _crafted(
        [],
        summary=False,
        top_level=[_schema(), _channel(), _message(data=b"\1" * (300 * KIB))],
    )
    with pytest.raises(McapLimitError, match="message"):
        _read_all(data, _limits())


def test_huge_record_length_is_rejected_before_reading_body():
    builder = RecordBuilder()
    Header(profile="ros2", library="test").write(builder)
    # Opcode 0x05 (Message) followed by a length of 1 TiB and no body.
    lie = struct.pack("<BQ", 0x05, 2**40)
    footer = _records(Footer(summary_start=0, summary_offset_start=0, summary_crc=0))
    data = MAGIC + builder.end() + _records(_schema(), _channel()) + lie + footer + MAGIC
    with pytest.raises(McapLimitError):
        _read_all(data, _limits())


def test_message_record_shorter_than_header_is_malformed_not_read_to_end():
    short = struct.pack("<BQ", 0x05, 4) + b"\0\0\0\0"
    raw = _records(_schema(), _channel()) + short
    data = _crafted([{"raw": raw}])
    with pytest.raises(McapError, match="shorter"):
        _read_all(data, _limits())


# ---------------------------------------------------------------------------
# 7: channel and schema counts
# ---------------------------------------------------------------------------


def test_too_many_summary_channels_is_rejected():
    definitions = [_schema()] + [_channel(channel_id=i, topic=f"/t{i}") for i in range(1, 10)]
    raw = _inner(_message())
    data = _crafted([{"raw": raw}], summary_definitions=definitions)
    with pytest.raises(McapLimitError, match="channels"):
        _read_all(data, _limits())


def test_too_many_schemas_without_summary_is_rejected():
    schemas = [_schema(schema_id=i) for i in range(1, 10)]
    data = _crafted([], summary=False, top_level=schemas)
    with pytest.raises(McapLimitError, match="schemas"):
        _read_all(data, _limits())


def test_redefining_one_channel_id_does_not_count_twice():
    raw = _inner(_message(data=b"a"))
    chunks = [{"raw": raw, "start": i} for i in range(12)]
    data = _crafted(chunks, summary=False)
    assert len(_read_all(data, _limits(max_channels=1, max_schemas=1))) == 12


# ---------------------------------------------------------------------------
# 9, 10: buffered messages for log-time ordering
# ---------------------------------------------------------------------------


def test_lying_chunk_start_times_cannot_buffer_the_whole_file():
    chunks = [
        {"raw": _inner(_message(log_time=10_000 + i, data=b"\0" * (200 * KIB))), "start": 0}
        for i in range(40)
    ]
    data = _crafted(chunks)
    with pytest.raises(McapLimitError, match="buffered"):
        _read_all(data, _limits())


def test_tiny_messages_count_their_object_overhead():
    raw = _inner(*[_message(log_time=10_000 + i, data=b"") for i in range(20_000)])
    data = _crafted([{"raw": raw, "start": 0}])
    with pytest.raises(McapLimitError, match="buffered"):
        _read_all(data, _limits(max_buffered_bytes=1 * MIB))


# ---------------------------------------------------------------------------
# 11, 12, 15, 16, 17: structure
# ---------------------------------------------------------------------------


def test_file_without_summary_streams_in_file_order():
    first = _inner(_message(log_time=50))
    second = _inner(_message(log_time=40), definitions=False)
    data = _crafted([{"raw": first}, {"raw": second}], summary=False)
    assert [row[1] for row in _read_all(data, _limits())] == [50, 40]


def test_chunk_index_outside_data_section_is_malformed():
    raw = _inner(_message())
    data = _crafted([{"raw": raw}], extra_index_offsets=[10**9])
    with pytest.raises(McapError, match="outside"):
        _read_all(data, _limits())


def test_chunk_index_pointing_at_non_chunk_is_malformed():
    raw = _inner(_message())
    data = _crafted([{"raw": raw}], extra_index_offsets=[len(MAGIC)])
    with pytest.raises(McapError, match="not a Chunk"):
        _read_all(data, _limits())


def test_truncated_file_without_footer_fails():
    data = _writer_file(compression=CompressionType.ZSTD)
    with pytest.raises(McapError):
        _read_all(data[: len(data) // 2], _limits())


def test_unknown_compression_fails():
    raw = _inner(_message())
    data = _crafted([{"raw": raw, "compression": "brotli", "payload": raw}])
    with pytest.raises(McapError, match="compression"):
        _read_all(data, _limits())


def test_large_attachment_is_skipped_not_rejected():
    attachment = Attachment(
        create_time=0,
        log_time=0,
        name="calibration.bin",
        media_type="application/octet-stream",
        data=b"\0" * (3 * MIB),
    )
    data = _crafted(
        [],
        summary=False,
        top_level=[_schema(), _channel(), attachment, _message(log_time=7)],
    )
    assert [row[1] for row in _read_all(data, _limits())] == [7]


# ---------------------------------------------------------------------------
# 20: configuration
# ---------------------------------------------------------------------------


def test_limits_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_CHUNK_BYTES", str(640 * MIB))
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_CHANNELS", "100")
    limits = McapLimits.from_env()
    assert limits.max_chunk_bytes == 640 * MIB
    assert limits.max_channels == 100
    assert limits.max_message_bytes == McapLimits().max_message_bytes


@pytest.mark.parametrize("value", ["0", "-1", "lots", "1.5"])
def test_limits_from_env_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_MESSAGE_BYTES", value)
    with pytest.raises(ValueError, match="PROCESSOR_MCAP_MAX_MESSAGE_BYTES"):
        McapLimits.from_env()


def test_message_cap_may_not_exceed_chunk_cap(monkeypatch):
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_CHUNK_BYTES", str(MIB))
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_MESSAGE_BYTES", str(2 * MIB))
    with pytest.raises(ValueError, match="PROCESSOR_MCAP_MAX_MESSAGE_BYTES"):
        McapLimits.from_env()


def test_run_once_validates_mcap_limits_before_claiming(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "check_cdr_decoder_available", lambda: None)
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_CHANNELS", "many")
    must_not_run = Mock(side_effect=AssertionError("claimed with bad config"))
    monkeypatch.setattr(core, "list_unprocessed_uploads", must_not_run)
    with pytest.raises(ValueError, match="PROCESSOR_MCAP_MAX_CHANNELS"):
        core.run_once(core.ProcessorConfig(parquet_root=tmp_path, topics=[]))


# ---------------------------------------------------------------------------
# 18, 19: a violation fails one upload terminally and the batch continues
# ---------------------------------------------------------------------------


def test_limit_violation_dead_letters_on_first_attempt_and_batch_continues(
    tmp_path, monkeypatch
):
    bomb = tmp_path / "bomb.mcap"
    bomb.write_bytes(
        _crafted([{"raw": b"", "payload": b"not zstd", "declared": 2 * MIB}])
    )
    good = tmp_path / "good.mcap"
    good.write_bytes(_writer_file(compression=CompressionType.ZSTD, count=3))
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_CHUNK_BYTES", str(MIB))
    monkeypatch.setenv("PROCESSOR_MCAP_MAX_MESSAGE_BYTES", str(256 * KIB))
    monkeypatch.setenv("SEGMENTATION_ENABLED", "false")
    attempt_a = "00000000-0000-0000-0000-00000000000a"
    attempt_b = "00000000-0000-0000-0000-00000000000b"
    candidates = [
        ("u-bomb", "robot", str(bomb), attempt_a, 1),
        ("u-good", "robot", str(good), attempt_b, 1),
    ]
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: None)
    monkeypatch.setattr(core, "_pg_prepare_claim", Mock())
    failure = Mock(return_value=1)
    complete = Mock()
    monkeypatch.setattr(core, "_pg_record_failure", failure)
    monkeypatch.setattr(core, "_pg_complete_claim", complete)

    processed = core._process_candidates(
        core.ProcessorConfig(parquet_root=tmp_path / "parquet", topics=[]),
        candidates,
    )

    assert processed == 1
    failure.assert_called_once()
    assert failure.call_args.args[0] == "u-bomb"
    assert failure.call_args.kwargs["terminal"] is True
    assert "McapLimitError" in failure.call_args.args[1]
    assert "uncompressed size" in failure.call_args.args[1]
    stages = failure.call_args.kwargs["metadata"]["stages"]
    assert stages["pipeline"]["status"] == "terminal_failure"
    complete.assert_called_once()
    assert complete.call_args.args[0] == "u-good"


def test_terminal_failure_sql_dead_letters_below_attempt_limit(monkeypatch):
    from test_processing_claims import FakeConnection, FakeCursor

    cursor = FakeCursor(fetchones=[(1,), (1,)])
    conn = FakeConnection(cursor)
    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core.psycopg, "connect", lambda _dsn: conn)

    core._pg_record_failure(
        "u-1",
        "McapLimitError: too big",
        attempt_id="00000000-0000-0000-0000-000000000001",
        terminal=True,
    )

    update = next(
        params
        for sql, params in cursor.statements
        if sql.startswith("UPDATE processing_status SET status = %s")
    )
    assert update[0] == "dead_letter"
    assert issubclass(McapLimitError, TerminalInputError)
