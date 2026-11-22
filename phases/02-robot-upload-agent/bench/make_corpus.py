#!/usr/bin/env python3
"""Deterministic MCAP corpus generator for the upload benchmarks.

Committed as a generator, not as binaries: the arms are hundreds of MB.

Two axes matter and the repo's existing fixtures only cover one corner of them:

* **Internal MCAP compression.** `development/seed.sh` and
  `tests/e2e/test_pipeline_e2e.py::_write_test_mcap` both write
  `compression=NONE`. Real recordings normally carry zstd or lz4 *chunk*
  compression -- that is the format's design. A compression benchmark run only
  against the seeded fixtures measures a ratio the fleet will never see, so
  `--compression {none,zstd,lz4}` is a first-class arm here.
* **Payload entropy.** The fixtures encode Imu messages as JSON, which is
  text-ish and compresses well. Real sensor topics are packed binary (CDR).
  `--payload {json,binary}` covers both ends so no claim rests on the friendlier
  one alone.

Usage:
  python make_corpus.py --out /tmp/bench --mib 500 --compression none --payload json
"""
import argparse
import json
import os
import random
import struct
from pathlib import Path

from mcap.writer import CompressionType, Writer

RAW_TOPIC = "/imu/data_raw"

_COMPRESSION = {
	"none": CompressionType.NONE,
	"zstd": CompressionType.ZSTD,
	"lz4": CompressionType.LZ4,
}


def _json_payload(i: int) -> bytes:
	return json.dumps({
		"linear_acceleration": {
			"x": round(0.1 + i % 97, 3),
			"y": round(0.2 + (i % 53) * 2, 3),
			"z": round(9.8 - (i % 31) * 0.01, 3),
		},
		"angular_velocity": {"x": round(i % 7 * 0.01, 4), "y": 0.0, "z": round(i % 11 * 0.02, 4)},
		"header": {"frame_id": "imu_link", "seq": i},
	}).encode("utf-8")


def _binary_payload(i: int, rng: random.Random) -> bytes:
	"""Packed doubles plus a noisy tail -- stands in for CDR sensor data.

	Deliberately NOT pure random: real IMU/lidar frames have a fixed header and
	slowly-varying fields, so a little structure is honest. The noisy tail keeps
	it from being trivially compressible the way JSON is.
	"""
	head = struct.pack(
		"<Qdddddd", i,
		0.1 + i % 97, 0.2 + (i % 53) * 2, 9.8 - (i % 31) * 0.01,
		i % 7 * 0.01, 0.0, i % 11 * 0.02,
	)
	return head + rng.randbytes(192)


def write_mcap(path: Path, target_bytes: int, compression: str, payload: str, seed: int = 1234) -> Path:
	rng = random.Random(seed)
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("wb") as f:
		writer = Writer(f, compression=_COMPRESSION[compression])
		writer.start()
		schema_id = writer.register_schema(
			name="sensor_msgs/msg/Imu",
			encoding="jsonschema" if payload == "json" else "ros2msg",
			data=b"{}",
		)
		channel_id = writer.register_channel(
			topic=RAW_TOPIC,
			message_encoding="json" if payload == "json" else "cdr",
			schema_id=schema_id,
		)
		base_ts = 1_700_000_000_000_000_000
		i = 0
		# Poll the real file size rather than estimating: with internal
		# compression on, message bytes and file bytes are not the same number,
		# and every arm must land on the same ON-DISK size or the throughput
		# comparison is measuring file size, not code.
		while True:
			for _ in range(2000):
				data = _json_payload(i) if payload == "json" else _binary_payload(i, rng)
				ts = base_ts + i * 1_000_000
				writer.add_message(channel_id=channel_id, log_time=ts, data=data, publish_time=ts)
				i += 1
			f.flush()
			if os.fstat(f.fileno()).st_size >= target_bytes:
				break
		writer.finish()
	return path


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--out", required=True)
	ap.add_argument("--mib", type=float, default=500.0)
	ap.add_argument("--compression", choices=sorted(_COMPRESSION), default="none")
	ap.add_argument("--payload", choices=("json", "binary"), default="json")
	ap.add_argument("--count", type=int, default=1, help="write N files (backlog arm)")
	ap.add_argument("--name", default=None)
	args = ap.parse_args()

	out = Path(args.out)
	target = int(args.mib * 1024 * 1024)
	for n in range(args.count):
		stem = args.name or f"{args.payload}_{args.compression}_{int(args.mib)}mib"
		suffix = "" if args.count == 1 else f"_{n:02d}"
		p = write_mcap(out / f"{stem}{suffix}.mcap", target, args.compression, args.payload, seed=1234 + n)
		# Back-date the mtime past upload_agent.min_stable_age_seconds so the
		# agent does not defer the file as "still recording".
		settled = 1_700_000_000
		os.utime(p, (settled, settled))
		print(f"{p} {p.stat().st_size}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
