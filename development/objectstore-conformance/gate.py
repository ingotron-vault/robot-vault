"""Verdict of the object-store conformance gate.

`python gate.py <results dir>` reads the artifacts of one run and exits 1 unless every required
step and probe is present and passed. A missing artifact, a skipped probe or an unknown
status is a failure, never a pass. Writes <results dir>/gate.json.

`REQUIRED_FEATURES` is the list of probes in conformance.py (which imports it from here, so the
two cannot drift). Plain standard library only, so the verdict can run anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_FEATURES = (
    "health endpoint answers 200",
    "bucket reachable, path style, region (ingest key)",
    "PutObject with the SDK default CRC32 checksum; HeadObject ETag is the MD5 (ingest key)",
    "Content-MD5 is validated: a bad MD5 gives 400 and stores nothing (ingest key)",
    "x-amz-checksum-sha256 is validated: a bad checksum gives 400 and stores nothing (ingest key)",
    "multipart resume after a client restart (ListParts), complete, ETag -N, ranged GET 206 (ingest key)",
    "ingest contract: MPU metadata kept, conditional writes and deletes give exactly 412 and change nothing, HEAD 404",
    "ListParts paginates beyond MaxParts (ingest key)",
    "ListMultipartUploads (gc key) shows Initiated; abort (ingest key) removes it; ListParts then gives 404",
    "ListObjectsV2 prefix, delimiter, pagination, odd keys, LastModified (processor writes, query lists)",
    "DeleteObjects with Quiet=False reports every key as Deleted (gc key)",
    "presigned GET (query key, public host): SigV4, Range 206, tampered 403, expired 403",
    "pyarrow S3FileSystem writes and reads a multipart-sized Parquet file (processor key)",
    "DuckDB httpfs secret and read_parquet glob (query key)",
    "denied writes and deletes give exactly 403 and leave bytes, ETag and metadata unchanged",
    "GC (real tools/s3_gc.py, gc key): dry run deletes nothing, a real run deletes only unreachable objects and orphan uploads",
    "backup (root key): rclone sync bucket to a folder and back to an empty bucket, rclone check --download, metadata kept",
)
# Steps that run.sh records in steps.txt, one "<name> <pass|fail...>" line each.
REQUIRED_STEPS = ("keys", "bootstrap", "expiry-red", "expiry-green", "store-argv", "durability-log", "bootstrap-argv",
                  "iam", "conformance", "contract-probe", "crash")
IAM_PROBES = 290
ACKNOWLEDGED_WRITES = 200
CONTRACT_CHECKS = 14


def load(directory: Path, name: str, problems: list[str]):
    path = directory / name
    if not path.is_file():
        problems.append(f"missing artifact {name}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        problems.append(f"unreadable artifact {name}: {e}")
        return None


def steps_of(directory: Path, problems: list[str]) -> dict[str, str]:
    path = directory / "steps.txt"
    if not path.is_file():
        problems.append("missing artifact steps.txt")
        return {}
    steps = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            name, _, status = line.partition(" ")
            steps[name] = status
    return steps


def verdict(directory: Path) -> list[str]:
    problems: list[str] = []
    steps = steps_of(directory, problems)
    for s in REQUIRED_STEPS:
        if s not in steps:
            problems.append(f"step {s}: not run")
        elif steps[s] != "pass":
            problems.append(f"step {s}: {steps[s]}")

    conf = load(directory, "conformance.json", problems)
    if conf is not None:
        seen = {}
        for r in conf.get("results", []):
            seen.setdefault(r.get("feature"), []).append(r.get("status"))
        for f in REQUIRED_FEATURES:
            if f not in seen:
                problems.append(f"probe missing: {f}")
            elif seen[f] != ["pass"]:
                problems.append(f"probe {seen[f]}: {f}")
        for f in seen.keys() - set(REQUIRED_FEATURES):
            problems.append(f"unknown probe in the artifact: {f}")

    iam = load(directory, "iam.json", problems)
    if iam is not None and not (iam.get("status") == "pass" and iam.get("probes") == IAM_PROBES and not any(
            iam.get(k) for k in ("over_permit", "under_permit", "errors"))):
        problems.append(f"iam: expected {IAM_PROBES} probes, 0 over, 0 under, 0 errors; got status={iam.get('status')} "
                        f"probes={iam.get('probes')}")

    argv = load(directory, "argv.json", problems)
    if argv is not None and argv.get("status") != "pass":
        problems.append("bootstrap argv proof did not pass")

    crash = load(directory, "crash-verify.json", problems)
    if crash is not None and not (crash.get("status") == "pass" and crash.get("acknowledged", 0) >= ACKNOWLEDGED_WRITES):
        problems.append(f"crash: status={crash.get('status')} acknowledged={crash.get('acknowledged')} "
                        f"problems={crash.get('problems')}")

    # The stale-upload expiry is proved red first (a 20 s expiry must abort the upload), then green.
    red = load(directory, "expiry-red.json", problems)
    if red is not None and red.get("status") != "fail":
        problems.append("expiry red proof: the upload survived a short expiry, so the setting is not shown to work")
    green = load(directory, "expiry-green.json", problems)
    if green is not None and green.get("status") != "pass":
        problems.append("expiry green: the upload did not survive the frozen 360h expiry")

    probe = load(directory, "contract-probe.json", problems)
    if probe is not None:
        bad = [r.get("check") for r in probe if not r.get("pass")]
        if len(probe) < CONTRACT_CHECKS or bad:
            problems.append(f"contract probe: {len(probe)} checks (need {CONTRACT_CHECKS}), failed: {bad}")
    return problems


def main(argv: list[str]) -> int:
    directory = Path(argv[1])
    problems = verdict(directory)
    status = "pass" if not problems else "fail"
    (directory / "gate.json").write_text(json.dumps({"status": status, "problems": problems}, indent=1))
    for p in problems:
        print(f"gate: {p}", file=sys.stderr)
    print(f"gate: {status.upper()} ({len(problems)} problem{'s' if len(problems) != 1 else ''})")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
