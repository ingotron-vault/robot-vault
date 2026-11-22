#!/usr/bin/env python3
"""Upload-throughput benchmark. Runs INSIDE the `dev` container.

Drives the real client path (`rsm scan` -> `rsm hash` -> `rua upload`) against
the running ingest server and reports client wall clock plus the server's own
`upload_started_at` -> `upload_completed_at` window from Postgres.

Two arms:
  --arm single   one large MCAP                       (U1, single-file throughput)
  --arm backlog  N MCAPs drained by one `rua upload --all` (U2, drain rate)

Everything is scoped by a unique `--robot-id`. `--cleanup-only` removes exactly
that robot's Postgres rows and registry entry and prints its S3 keys for
tools/s3_gc.py -- nothing else in the shared database is touched.
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://robotvault:robotvault@postgres:5432/robotvault")
INGEST_ADMIN = os.environ.get("INGEST_ADMIN_URL", "http://ingest:8080")
TOKEN = os.environ.get("INGEST_AUTH_TOKEN", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://objectstore:9000")
S3_BUCKET = os.environ.get("S3_BUCKET", "robotics-data")


def _run(cmd, timeout=1800):
	return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def register_robot(robot_id: str) -> None:
	import httpx
	r = httpx.post(
		f"{INGEST_ADMIN}/admin/robots",
		params={"robot_id": robot_id, "name": f"upload bench {robot_id}"},
		headers={"authorization": f"Bearer {TOKEN}"} if TOKEN else {},
		timeout=15,
	)
	if r.status_code != 200:
		raise SystemExit(f"robot registration failed: {r.status_code} {r.text}")


def write_config(dest: Path, robot_id: str, base_config: Path, overrides: dict) -> Path:
	cfg = json.loads(base_config.read_text())
	cfg["robot_id"] = robot_id
	cfg.setdefault("upload_agent", {}).update(overrides)
	# The corpus generator back-dates mtimes, but be explicit: this harness
	# never wants the still-recording gate to defer a file.
	cfg["upload_agent"]["min_stable_age_seconds"] = 0
	dest.parent.mkdir(parents=True, exist_ok=True)
	dest.write_text(json.dumps(cfg, indent=2))
	return dest


def validate_parallel_stream_preflight(status: dict) -> dict:
	"""Fail before a benchmark cell can mislabel a server-capped stream arm."""

	requested = int(status.get("configured_parallel_streams") or 0)
	server_cap = int(status.get("server_per_robot_streams") or 0)
	server_source = status.get("server_streams_source")
	effective = int(status.get("effective_parallel_streams") or 0)
	if requested < 1 or server_cap < 1 or effective < 1:
		raise SystemExit(
			"benchmark validity preflight could not resolve requested, server, "
			f"and effective streams from rua status: {status!r}"
		)
	if server_source != "advertised":
		raise SystemExit(
			"benchmark validity preflight requires ingest's advertised stream cap; "
			f"rua reported source={server_source!r}"
		)
	if requested > server_cap:
		raise SystemExit(
			"benchmark validity preflight: requested parallel_streams="
			f"{requested} exceeds ingest PER_ROBOT_CONCURRENCY={server_cap}; "
			"raise BENCH_PER_ROBOT_CONCURRENCY or lower the requested arm"
		)
	return {
		"requested_parallel_streams": requested,
		"server_per_robot_streams": server_cap,
		"server_streams_source": server_source,
		"effective_parallel_streams": effective,
		"connection_quality": status.get("connection_quality"),
		"connection_measurements": status.get("connection_measurements") or {},
		"strategy_parallel_streams": int(
			(status.get("batch_strategy") or {}).get("parallel_streams") or 0
		),
	}


def benchmark_preflight(root: Path, cfg: Path) -> dict:
	status_result = _run(
		[
			"rua", "status", "--probe-server", "--config", str(cfg),
			"--root", str(root),
		],
		timeout=30,
	)
	if status_result.returncode != 0:
		raise SystemExit(
			"benchmark validity preflight could not query rua status: "
			f"{status_result.stderr}\n{status_result.stdout}"
		)
	try:
		status = json.loads(status_result.stdout)
	except json.JSONDecodeError as exc:
		raise SystemExit(
			f"benchmark validity preflight got non-JSON rua status: {exc}: "
			f"{status_result.stdout!r}"
		) from exc
	return validate_parallel_stream_preflight(status)


def server_window(robot_id: str) -> dict:
	import psycopg
	with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
		cur.execute(
			"""
			SELECT count(*),
			       count(*) FILTER (WHERE status = 'completed'),
			       count(*) FILTER (WHERE integrity_verified),
			       coalesce(sum(bytes_received), 0),
			       coalesce(sum(extract(epoch FROM (upload_completed_at - upload_started_at))), 0),
			       coalesce(extract(epoch FROM (max(upload_completed_at) - min(upload_started_at))), 0)
			FROM uploads WHERE robot_id = %s
			""",
			(robot_id,),
		)
		n, completed, verified, bytes_recv, sum_secs, span_secs = cur.fetchone()
	return {
		"rows": n, "completed": completed, "integrity_verified": verified,
		"bytes_received": int(bytes_recv), "server_sum_seconds": round(float(sum_secs), 3),
		"server_span_seconds": round(float(span_secs), 3),
	}


def cleanup(robot_id: str) -> dict:
	"""Delete exactly this robot's Postgres footprint and report its S3 keys.

	Deliberately does NOT delete S3 objects. `tools/s3_gc.py` is the single
	owner of object deletion in this repo (there is a unit test
	asserting no second sweeper exists), and a benchmark harness is exactly the
	kind of "small convenience deleter" that invariant exists to prevent. The
	keys are printed instead, so an operator can hand them to the GC or remove
	them by hand once.
	"""
	import psycopg
	orphan_keys = []
	with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
		cur.execute("SELECT id, storage_path FROM uploads WHERE robot_id = %s", (robot_id,))
		rows = cur.fetchall()
		for _id, path in rows:
			if path:
				orphan_keys.append(path)
		ids = [r[0] for r in rows]
		if ids:
			cur.execute("DELETE FROM processing_status WHERE upload_id = ANY(%s)", (ids,))
		cur.execute("DELETE FROM uploads WHERE robot_id = %s", (robot_id,))
		cur.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
		conn.commit()
	if orphan_keys:
		# Files at/under `small_file_threshold_mb` go through `upload_small`,
		# which is content-addressed (CAS) server-side, so identical files
		# collapse to one S3 key. Files above the threshold go through the
		# streaming/MPU path, which keys on `upload_id` and is NOT
		# content-addressed -- identical files still get distinct objects.
		# So the count below is genuinely the number of unreferenced keys;
		# only the printed sample is capped, and it must say so, or "8
		# object(s): [4 keys]" reads as a miscount rather than a preview.
		unique_keys = sorted(set(orphan_keys))
		shown = unique_keys[:4]
		sample_note = "" if len(shown) == len(unique_keys) else f" (showing first {len(shown)})"
		print(f"  note: {len(unique_keys)} S3 object(s) now unreferenced "
		      f"(left for tools/s3_gc.py){sample_note}: {shown}", file=sys.stderr)
	return {"upload_rows_deleted": len(rows), "s3_keys_left_for_gc": sorted(set(orphan_keys))}


def reset_manifests(root: Path) -> None:
	for m in root.rglob("*.manifest.json"):
		m.unlink()
	for m in root.rglob(".manifests"):
		if m.is_dir():
			shutil.rmtree(m)


def one_trial(
	root: Path, cfg: Path, robot_id: str, mode: str, validity_preflight: dict
) -> dict:
	reset_manifests(root)
	cleanup(robot_id)
	register_robot(robot_id)
	scan = _run(["rsm", "scan", "--config", str(cfg), "--root", str(root)])
	if scan.returncode != 0:
		raise SystemExit(f"rsm scan failed: {scan.stderr}")
	h = _run(["rsm", "hash", "--config", str(cfg), "--root", str(root)])
	if h.returncode != 0:
		raise SystemExit(f"rsm hash failed: {h.stderr}")
	base = ["rua", "upload", "--config", str(cfg), "--root", str(root)]
	invocations = 0
	if mode == "backlog-loop":
		# The one-file-per-invocation baseline for U2: `rua upload` is structurally
		# single-file (cli.py calls run_once, which uploads exactly one file and
		# returns), so draining a backlog means re-invoking the CLI per file --
		# paying the fixed Python start cost every time.
		t0 = time.perf_counter()
		while True:
			up = _run(base)
			invocations += 1
			if up.returncode != 0:
				raise SystemExit(f"rua upload failed: {up.stderr}\n{up.stdout}")
			result = json.loads(up.stdout)
			if not result.get("uploaded"):
				break
		wall = time.perf_counter() - t0
	else:
		cmd = list(base) + (["--all"] if mode == "backlog" else [])
		t0 = time.perf_counter()
		up = _run(cmd)
		wall = time.perf_counter() - t0
		invocations = 1
		if up.returncode != 0:
			raise SystemExit(f"rua upload failed: {up.stderr}\n{up.stdout}")
		result = json.loads(up.stdout)
	win = server_window(robot_id)
	total_bytes = sum(p.stat().st_size for p in sorted(root.rglob("*.mcap")))
	actual_streams = (
		int(result.get("effective_parallel_streams") or 0)
		if mode == "backlog"
		else 1 if result.get("uploaded") else 0
	)
	return {
		"wall_seconds": round(wall, 3),
		"mib_per_s": round((total_bytes / (1024 * 1024)) / wall, 1),
		"bytes": total_bytes,
		"rua_result": result,
		"stream_bounds": {
			**validity_preflight,
			"actual_effective_parallel_streams": actual_streams,
		},
		"cli_invocations": invocations,
		**win,
	}


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--root", required=True, help="directory holding the corpus")
	ap.add_argument("--robot-id", required=True)
	ap.add_argument("--arm", choices=("single", "backlog", "backlog-loop"), default="single")
	ap.add_argument("--trials", type=int, default=3)
	ap.add_argument("--base-config", default="/app/configs/vault.local.json")
	ap.add_argument("--set", action="append", default=[], metavar="K=V",
	                help="upload_agent override, e.g. --set chunk_size_bytes=16777216")
	ap.add_argument("--label", default="")
	ap.add_argument("--cleanup-only", action="store_true")
	args = ap.parse_args()

	root = Path(args.root)
	if args.cleanup_only:
		print(json.dumps(cleanup(args.robot_id), indent=2))
		return 0

	overrides = {}
	for kv in args.set:
		k, _, v = kv.partition("=")
		try:
			overrides[k] = json.loads(v)
		except Exception:
			overrides[k] = v
	cfg = write_config(root / "vault.bench.json", args.robot_id, Path(args.base_config), overrides)
	preflight = benchmark_preflight(root, cfg)
	print(
		"  validity: quality={connection_quality} requested={requested_parallel_streams} "
		"strategy={strategy_parallel_streams} server={server_per_robot_streams}"
		"({server_streams_source}) "
		"effective={effective_parallel_streams} measurements={connection_measurements}".format(
			**preflight
		)
	)

	trials = []
	for i in range(args.trials):
		t = one_trial(root, cfg, args.robot_id, args.arm, preflight)
		print(f"  trial {i + 1}: {t['wall_seconds']}s  {t['mib_per_s']} MiB/s  "
		      f"server_span={t['server_span_seconds']}s  completed={t['completed']}/{t['rows']} "
		      f"verified={t['integrity_verified']}")
		trials.append(t)

	rates = [t["mib_per_s"] for t in trials]
	walls = [t["wall_seconds"] for t in trials]
	out = {
		"label": args.label, "arm": args.arm, "robot_id": args.robot_id,
		"overrides": overrides, "bytes": trials[0]["bytes"],
		"validity_preflight": preflight,
		"files": len(list(root.rglob("*.mcap"))),
		"wall_seconds": {"median": statistics.median(walls), "all": walls},
		"mib_per_s": {"median": statistics.median(rates), "all": rates},
		"server_span_seconds": [t["server_span_seconds"] for t in trials],
		"all_completed": all(t["completed"] == t["rows"] and t["rows"] > 0 for t in trials),
		"all_integrity_verified": all(t["integrity_verified"] == t["rows"] for t in trials),
		"trials": trials,
	}
	print(json.dumps(out, indent=2))
	cleanup(args.robot_id)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
