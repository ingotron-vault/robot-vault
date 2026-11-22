"""Read MCAP messages with hard memory limits.

The mcap library (1.4.0) trusts the sizes inside the file:

* ``zstandard.decompress(chunk.data, chunk.uncompressed_size)`` allocates the
  content size from the zstd FRAME header, not the size in the Chunk record.
  A small chunk can therefore expand to many gigabytes.
* ``lz4.frame.decompress`` has no output limit at all.
* With log-time ordering, ``SeekingReader`` decompresses every chunk whose
  index start time is earliest. A summary that lies about start times makes it
  hold the whole file in memory.
* A file without a summary goes through ``NonSeekingReader``, which sorts all
  messages of the file in memory.
* A Message record shorter than its 22-byte header makes ``Message.read``
  call ``read(negative)``, which reads to the end of the stream.

This reader keeps the library's record classes and its message order for files
with chunk indexes, but it checks every size before it allocates memory. The
checks use the Chunk record itself, never only the summary index, because the
index can lie. A file without chunk indexes is read once, in file order.

A limit violation raises :class:`McapLimitError`. The processor treats it as
terminal: the same bytes would fail the same way on every retry.
"""

from __future__ import annotations

import heapq
import io
import os
import struct
from dataclasses import dataclass, fields
from typing import BinaryIO, Dict, Iterator, List, Optional, Set, Tuple

import lz4.frame
import zstandard
from mcap.data_stream import ReadDataStream
from mcap.exceptions import EndOfFile, McapError, UnsupportedCompressionError
from mcap.opcode import Opcode
from mcap.records import Channel, Message, Schema

MAGIC = b"\x89MCAP0\r\n"
_FOOTER_BODY = 8 + 8 + 4
_RECORD_HEADER = 1 + 8
_FOOTER_RECORD = _RECORD_HEADER + _FOOTER_BODY
_MESSAGE_HEADER = 2 + 4 + 8 + 8
# Approximate Python cost of one buffered message (Message object, bytes
# header, heap tuple). Counted with the data so that many empty messages
# cannot pass the buffer limit.
MESSAGE_OVERHEAD_BYTES = 320
# Schema and Channel records are small (a ros2msg text, a topic name). Their
# total size is fixed rather than configurable.
MAX_DEFINITION_BYTES = 64 * 1024 * 1024
_READ_STEP = 8 * 1024 * 1024

MIB = 1024 * 1024


class TerminalInputError(Exception):
    """The upload content can never be processed. Do not retry it."""


class McapLimitError(TerminalInputError):
    """The MCAP file exceeds a processor memory limit."""


_ENV_NAMES = {
    "max_chunk_bytes": "PROCESSOR_MCAP_MAX_CHUNK_BYTES",
    "max_message_bytes": "PROCESSOR_MCAP_MAX_MESSAGE_BYTES",
    "max_channels": "PROCESSOR_MCAP_MAX_CHANNELS",
    "max_schemas": "PROCESSOR_MCAP_MAX_SCHEMAS",
    "max_buffered_bytes": "PROCESSOR_MCAP_MAX_BUFFERED_BYTES",
}


@dataclass(frozen=True)
class McapLimits:
    """Per-upload MCAP limits. Defaults are far above real robot recordings.

    rosbag2 and Foxglove write chunks of about 1 MiB. A single message above
    256 MiB (for example a full-resolution raw 8K frame is about 100 MiB) is
    not expected. Peak reader memory is about two chunks plus the buffer.
    """

    # Limit for one Chunk record: both its stored size and its declared and
    # actual decompressed size.
    max_chunk_bytes: int = 512 * MIB
    max_message_bytes: int = 256 * MIB
    max_channels: int = 4096
    max_schemas: int = 4096
    # Messages decompressed but not yet yielded while the reader merges
    # chunks in log-time order (data plus MESSAGE_OVERHEAD_BYTES each).
    max_buffered_bytes: int = 1024 * MIB

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    f"{_ENV_NAMES[field.name]} must be a positive integer, got {value!r}"
                )
        if self.max_message_bytes > self.max_chunk_bytes:
            raise ValueError(
                "PROCESSOR_MCAP_MAX_MESSAGE_BYTES must not exceed "
                "PROCESSOR_MCAP_MAX_CHUNK_BYTES"
            )

    @classmethod
    def from_env(cls) -> "McapLimits":
        values: Dict[str, int] = {}
        for name, env in _ENV_NAMES.items():
            raw = os.environ.get(env)
            if raw is None or raw.strip() == "":
                continue
            try:
                values[name] = int(raw.strip(), 10)
            except ValueError:
                raise ValueError(
                    f"{env} must be a positive integer, got {raw!r}"
                ) from None
        return cls(**values)


def make_reader(stream: BinaryIO, *, limits: Optional[McapLimits] = None) -> "BoundedMcapReader":
    """Return a reader for a seekable MCAP stream."""

    return BoundedMcapReader(stream, limits or McapLimits())


class BoundedMcapReader:
    def __init__(self, stream: BinaryIO, limits: McapLimits) -> None:
        self._stream = stream
        self._limits = limits
        self._schemas: Dict[int, Schema] = {}
        self._channels: Dict[int, Channel] = {}
        self._definition_bytes = 0

    # -- low-level reads --------------------------------------------------

    def _read_exact(self, length: int) -> bytes:
        if length == 0:
            return b""
        data = self._stream.read(length)
        if len(data) != length:
            raise EndOfFile()
        return data

    def _read_record_header(self) -> Tuple[int, int]:
        opcode, length = struct.unpack("<BQ", self._read_exact(_RECORD_HEADER))
        return opcode, length

    # -- definitions ------------------------------------------------------

    def _check_definition_size(self, opcode: int, length: int) -> None:
        if length > MAX_DEFINITION_BYTES:
            raise McapLimitError(
                f"MCAP {Opcode(opcode).name} record of {length} bytes exceeds "
                f"the {MAX_DEFINITION_BYTES}-byte definition limit"
            )

    def _add_definition(self, opcode: int, body: bytes) -> None:
        stream = io.BytesIO(body)
        if opcode == Opcode.SCHEMA:
            schema = Schema.read(ReadDataStream(stream))
            if schema.id not in self._schemas:
                if len(self._schemas) >= self._limits.max_schemas:
                    raise McapLimitError(
                        f"MCAP declares more than {self._limits.max_schemas} schemas "
                        f"(PROCESSOR_MCAP_MAX_SCHEMAS)"
                    )
                self._charge_definition(len(body))
            self._schemas[schema.id] = schema
        else:
            channel = Channel.read(ReadDataStream(stream))
            if channel.id not in self._channels:
                if len(self._channels) >= self._limits.max_channels:
                    raise McapLimitError(
                        f"MCAP declares more than {self._limits.max_channels} channels "
                        f"(PROCESSOR_MCAP_MAX_CHANNELS)"
                    )
                self._charge_definition(len(body))
            self._channels[channel.id] = channel

    def _charge_definition(self, size: int) -> None:
        self._definition_bytes += size
        if self._definition_bytes > MAX_DEFINITION_BYTES:
            raise McapLimitError(
                f"MCAP schema and channel records exceed {MAX_DEFINITION_BYTES} bytes"
            )

    # -- messages ---------------------------------------------------------

    def _check_message_length(self, length: int) -> None:
        if length < _MESSAGE_HEADER:
            raise McapError(
                f"MCAP Message record of {length} bytes is shorter than its "
                f"{_MESSAGE_HEADER}-byte header"
            )
        if length - _MESSAGE_HEADER > self._limits.max_message_bytes:
            raise McapLimitError(
                f"MCAP message of {length - _MESSAGE_HEADER} bytes exceeds the "
                f"{self._limits.max_message_bytes}-byte message limit "
                f"(PROCESSOR_MCAP_MAX_MESSAGE_BYTES)"
            )

    @staticmethod
    def _parse_message(body) -> Message:
        channel_id, sequence, log_time, publish_time = struct.unpack_from(
            "<HIQQ", body, 0
        )
        return Message(
            channel_id=channel_id,
            sequence=sequence,
            log_time=log_time,
            publish_time=publish_time,
            data=bytes(body[_MESSAGE_HEADER:]),
        )

    def _resolve(self, message: Message) -> Tuple[Optional[Schema], Channel, Message]:
        channel = self._channels.get(message.channel_id)
        if channel is None:
            raise McapError(f"no channel record found with id {message.channel_id}")
        if channel.schema_id == 0:
            return None, channel, message
        schema = self._schemas.get(channel.schema_id)
        if schema is None:
            raise McapError(f"no schema record found with id {channel.schema_id}")
        return schema, channel, message

    # -- chunks -----------------------------------------------------------

    def _check_chunk_record_length(self, length: int, offset: int) -> None:
        if length > self._limits.max_chunk_bytes:
            raise McapLimitError(
                f"MCAP Chunk record at offset {offset} is {length} bytes, above "
                f"the {self._limits.max_chunk_bytes}-byte chunk limit "
                f"(PROCESSOR_MCAP_MAX_CHUNK_BYTES)"
            )

    def _decompress_chunk(self, body: bytes, offset: int) -> memoryview:
        view = memoryview(body)
        try:
            (_start, _end, uncompressed_size, _crc, compression_length) = (
                struct.unpack_from("<QQQII", view, 0)
            )
            position = 8 + 8 + 8 + 4 + 4
            compression = str(view[position : position + compression_length], "utf-8")
            position += compression_length
            (data_length,) = struct.unpack_from("<Q", view, position)
            position += 8
        except (struct.error, UnicodeDecodeError) as exc:
            raise McapError(f"malformed MCAP Chunk record at offset {offset}") from exc
        if data_length > len(view) - position:
            raise McapError(
                f"MCAP Chunk at offset {offset} declares {data_length} data bytes "
                f"but its record holds {len(view) - position}"
            )
        data = view[position : position + data_length]
        cap = self._limits.max_chunk_bytes
        if compression == "":
            return data
        if uncompressed_size > cap:
            raise McapLimitError(
                f"MCAP Chunk at offset {offset} declares an uncompressed size of "
                f"{uncompressed_size} bytes, above the {cap}-byte chunk limit "
                f"(PROCESSOR_MCAP_MAX_CHUNK_BYTES)"
            )
        # Read at most one byte more than declared. The frame header's content
        # size is never used to size a buffer.
        wanted = uncompressed_size + 1
        if compression == "zstd":
            try:
                reader = zstandard.ZstdDecompressor().stream_reader(
                    io.BytesIO(data), read_across_frames=True
                )
                out = bytearray()
                while len(out) < wanted:
                    piece = reader.read(min(_READ_STEP, wanted - len(out)))
                    if not piece:
                        break
                    out += piece
            except zstandard.ZstdError as exc:
                raise McapError(
                    f"MCAP Chunk at offset {offset} has invalid zstd data: {exc}"
                ) from exc
        elif compression == "lz4":
            try:
                out = lz4.frame.LZ4FrameDecompressor().decompress(
                    bytes(data), max_length=wanted
                )
            except RuntimeError as exc:
                raise McapError(
                    f"MCAP Chunk at offset {offset} has invalid lz4 data: {exc}"
                ) from exc
        else:
            raise UnsupportedCompressionError(compression)
        if len(out) > uncompressed_size:
            raise McapLimitError(
                f"MCAP Chunk at offset {offset} decompresses to more than its "
                f"declared uncompressed size of {uncompressed_size} bytes"
            )
        return memoryview(out)

    def _iter_chunk_records(self, records: memoryview, offset: int) -> Iterator[Tuple[int, memoryview]]:
        position = 0
        total = len(records)
        while position < total:
            if total - position < _RECORD_HEADER:
                raise McapError(f"truncated record inside MCAP Chunk at offset {offset}")
            opcode, length = struct.unpack_from("<BQ", records, position)
            position += _RECORD_HEADER
            if length > total - position:
                raise McapError(
                    f"record inside MCAP Chunk at offset {offset} overruns the chunk"
                )
            yield opcode, records[position : position + length]
            position += length

    # -- public API -------------------------------------------------------

    def iter_messages(self) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        stream = self._stream
        stream.seek(0)
        if self._read_exact(len(MAGIC)) != MAGIC:
            raise McapError("not a valid MCAP file: invalid magic")
        end = stream.seek(0, io.SEEK_END)
        footer_offset = end - len(MAGIC) - _FOOTER_RECORD
        if footer_offset < len(MAGIC):
            raise McapError("MCAP file is too short to hold a footer")
        stream.seek(footer_offset)
        opcode, length = self._read_record_header()
        if opcode != Opcode.FOOTER or length != _FOOTER_BODY:
            raise McapError("expected footer at end of MCAP file")
        summary_start, _summary_offset_start, _crc = struct.unpack(
            "<QQI", self._read_exact(_FOOTER_BODY)
        )
        if self._read_exact(len(MAGIC)) != MAGIC:
            raise McapError("not a valid MCAP file: invalid closing magic")

        if summary_start:
            if not len(MAGIC) <= summary_start <= footer_offset:
                raise McapError("MCAP footer points outside the file")
            chunk_starts = self._read_summary(summary_start, footer_offset)
            if chunk_starts:
                yield from self._iter_indexed(chunk_starts, summary_start)
                return
            # Like the mcap library: without chunk indexes, read linearly and
            # learn the definitions from the data section.
            self._schemas.clear()
            self._channels.clear()
            self._definition_bytes = 0
        yield from self._iter_linear(footer_offset)

    def _read_summary(self, start: int, stop: int) -> List[Tuple[int, int]]:
        """Read summary definitions and return ``(start_time, offset)`` per chunk."""

        stream = self._stream
        stream.seek(start)
        position = start
        chunk_starts: List[Tuple[int, int]] = []
        while position < stop:
            opcode, length = self._read_record_header()
            position += _RECORD_HEADER
            if length > stop - position:
                raise McapError("MCAP summary record overruns the summary section")
            if opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
                self._check_definition_size(opcode, length)
                self._add_definition(opcode, self._read_exact(length))
            elif opcode == Opcode.CHUNK_INDEX:
                if length < 32:
                    raise McapError("MCAP ChunkIndex record is too short")
                message_start_time, _end, chunk_offset = struct.unpack(
                    "<QQQ", self._read_exact(24)
                )
                chunk_starts.append((message_start_time, chunk_offset))
                stream.seek(length - 24, io.SEEK_CUR)
            else:
                stream.seek(length, io.SEEK_CUR)
            position += length
        return chunk_starts

    def _iter_indexed(
        self, chunk_starts: List[Tuple[int, int]], data_end: int
    ) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        """Merge chunks in log-time order, as ``SeekingReader`` does.

        Heap keys are ``(log_time, chunk_offset, record_index)``; a chunk
        entry uses index -1. That reproduces the library's tie-break.
        """

        heap: List[Tuple[int, int, int, Optional[Message]]] = []
        seen: Set[int] = set()
        for start_time, offset in chunk_starts:
            if offset in seen:
                continue
            seen.add(offset)
            if not len(MAGIC) <= offset < data_end:
                raise McapError(
                    f"MCAP chunk index points outside the data section (offset {offset})"
                )
            heap.append((start_time, offset, -1, None))
        heapq.heapify(heap)
        buffered = 0
        cap = self._limits.max_buffered_bytes
        while heap:
            _time, offset, index, message = heapq.heappop(heap)
            if message is not None:
                buffered -= len(message.data) + MESSAGE_OVERHEAD_BYTES
                yield self._resolve(message)
                continue
            self._stream.seek(offset)
            opcode, length = self._read_record_header()
            if opcode != Opcode.CHUNK:
                raise McapError(
                    f"MCAP chunk index at offset {offset} points at a record that "
                    f"is not a Chunk"
                )
            self._check_chunk_record_length(length, offset)
            records = self._decompress_chunk(self._read_exact(length), offset)
            record_index = 0
            for inner_opcode, body in self._iter_chunk_records(records, offset):
                if inner_opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
                    # Summary definitions are authoritative here, as in the
                    # library's SeekingReader; these only keep its numbering.
                    record_index += 1
                    continue
                if inner_opcode != Opcode.MESSAGE:
                    continue
                self._check_message_length(len(body))
                parsed = self._parse_message(body)
                buffered += len(parsed.data) + MESSAGE_OVERHEAD_BYTES
                if buffered > cap:
                    raise McapLimitError(
                        f"MCAP log-time merge buffered more than {cap} bytes of "
                        f"messages (PROCESSOR_MCAP_MAX_BUFFERED_BYTES); the chunk "
                        f"index start times do not match the messages"
                    )
                heapq.heappush(heap, (parsed.log_time, offset, record_index, parsed))
                record_index += 1
            del records

    def _iter_linear(self, stop: int) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        """Read the data section once, in file order, one record at a time."""

        stream = self._stream
        stream.seek(len(MAGIC))
        position = len(MAGIC)
        while position < stop:
            opcode, length = self._read_record_header()
            record_offset = position
            position += _RECORD_HEADER
            if opcode in (Opcode.DATA_END, Opcode.FOOTER):
                return
            if opcode == Opcode.CHUNK:
                self._check_chunk_record_length(length, record_offset)
            elif opcode == Opcode.MESSAGE:
                self._check_message_length(length)
            elif opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
                self._check_definition_size(opcode, length)
            if length > stop - position:
                raise McapError(
                    f"MCAP record at offset {record_offset} overruns the data section"
                )
            if opcode == Opcode.CHUNK:
                records = self._decompress_chunk(self._read_exact(length), record_offset)
                for inner_opcode, body in self._iter_chunk_records(records, record_offset):
                    if inner_opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
                        self._add_definition(inner_opcode, bytes(body))
                    elif inner_opcode == Opcode.MESSAGE:
                        self._check_message_length(len(body))
                        yield self._resolve(self._parse_message(body))
                del records
            elif opcode == Opcode.MESSAGE:
                yield self._resolve(self._parse_message(self._read_exact(length)))
            elif opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
                self._add_definition(opcode, self._read_exact(length))
            else:
                # Header, attachments, metadata, indexes: never loaded.
                stream.seek(length, io.SEEK_CUR)
            position += length
