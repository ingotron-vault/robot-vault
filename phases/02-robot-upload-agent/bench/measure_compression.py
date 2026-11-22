#!/usr/bin/env python3
"""Measure whether compressing an MCAP on the upload path pays.

Measured BEFORE any compression feature was written, on the unit the wire
actually carries: one `chunk_size_bytes` slice of the file (gRPC compresses
per message, so per-chunk is the honest granularity).

For each corpus arm it reports, per codec:
  ratio       = uncompressed_bytes / compressed_bytes  (>1 means a win)
  MiB/s       = robot-side compressor throughput
  break-even  = the link speed below which compression is free, i.e. the speed
                at which the bytes saved take longer to send than the CPU spent
                saving them.

Also prints the authoritative internal-compression signal: ChunkIndex.compression
from the MCAP summary section (op 0x08), which is what detection must key on.
"""
import argparse
import gzip
import time
import zlib
from pathlib import Path

CHUNK = 16 * 1024 * 1024


def summary_chunk_compression(path: Path) -> set:
	"""ChunkIndex.compression values from the summary section.

	The authoritative answer to "is this MCAP internally compressed": every
	ChunkIndex record duplicates its Chunk's `compression` string in the
	summary, so a reader learns the answer without touching chunk bodies.
	"""
	from mcap.reader import make_reader
	with path.open("rb") as f:
		reader = make_reader(f)
		summary = reader.get_summary()
		if summary is None:
			return {"<no-summary>"}
		return {ci.compression for ci in summary.chunk_indexes} or {"<no-chunk-indexes>"}


def _time_codec(fn, blocks):
	t0 = time.perf_counter()
	out = sum(len(fn(b)) for b in blocks)
	return out, time.perf_counter() - t0


def measure(path: Path, chunk_size: int, max_chunks: int) -> dict:
	blocks = []
	with path.open("rb") as f:
		while len(blocks) < max_chunks:
			b = f.read(chunk_size)
			if not b:
				break
			blocks.append(b)
	raw = sum(len(b) for b in blocks)

	codecs = {
		# gzip level 6 == python's default and gRPC's Gzip compressor.
		"gzip-6": lambda b: gzip.compress(b, 6),
		"gzip-1": lambda b: gzip.compress(b, 1),
		"deflate-6": lambda b: zlib.compress(b, 6),
	}
	try:
		import zstandard
		codecs["zstd-3"] = lambda b: zstandard.ZstdCompressor(level=3).compress(b)
	except Exception:
		pass

	rows = {}
	for name, fn in codecs.items():
		out, secs = _time_codec(fn, blocks)
		mibs = (raw / (1024 * 1024)) / secs if secs else float("inf")
		saved_bytes = raw - out
		# Break-even link speed: below this, the seconds saved on the wire
		# exceed the seconds burned compressing.
		break_even_mbps = (saved_bytes * 8 / (1024 * 1024)) / secs if secs else float("inf")
		rows[name] = {
			"ratio": round(raw / out, 4) if out else 0.0,
			"saved_pct": round(100.0 * saved_bytes / raw, 2) if raw else 0.0,
			"compress_mibs": round(mibs, 1),
			"cpu_seconds_per_gib": round(secs / (raw / (1024 ** 3)), 2) if raw else 0.0,
			"break_even_link_mbps": round(break_even_mbps, 1),
		}
	return {
		"file": str(path),
		"file_size_bytes": path.stat().st_size,
		"measured_bytes": raw,
		"chunks": len(blocks),
		"chunk_size_bytes": chunk_size,
		"summary_chunk_compression": sorted(summary_chunk_compression(path)),
		"codecs": rows,
	}


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("files", nargs="+")
	ap.add_argument("--chunk-size", type=int, default=CHUNK)
	ap.add_argument("--max-chunks", type=int, default=4)
	args = ap.parse_args()
	for p in args.files:
		r = measure(Path(p), args.chunk_size, args.max_chunks)
		print(f"\n=== {r['file']}  ({r['file_size_bytes']/1048576:.1f} MiB on disk)")
		print(f"    summary ChunkIndex.compression = {r['summary_chunk_compression']}")
		print(f"    measured {r['chunks']} x {r['chunk_size_bytes']/1048576:.0f} MiB = {r['measured_bytes']/1048576:.1f} MiB")
		print(f"    {'codec':<10} {'ratio':>7} {'saved%':>8} {'MiB/s':>8} {'cpu s/GiB':>10} {'break-even Mbps':>16}")
		for name, row in r["codecs"].items():
			print(f"    {name:<10} {row['ratio']:>7.3f} {row['saved_pct']:>8.2f} "
			      f"{row['compress_mibs']:>8.1f} {row['cpu_seconds_per_gib']:>10.2f} {row['break_even_link_mbps']:>16.1f}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
