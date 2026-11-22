#!/usr/bin/env python3
"""End-to-end proof for `make backup` / `make restore` (tools/robot_vault_backup.py) on RustFS.

Runs on the host (stdlib only) against a disposable dev Compose stack; see run.sh. It drives
the real Make targets and checks the results with probe.py (dev image, on the stack network).

Ways backup and restore could fail, and the case that detects each:

  Metadata manifest
    fresh-storage    the recovery point is copied WITHOUT extended attributes (as to other storage),
                     the original is deleted, the database and the store data are lost; the restore
                     must give back every table, every object byte, content type and user metadata
                     (rdv-resume-session on a 3-part multipart object, odd keys, an empty object).
    bucket-damaged   after the backup: same-size wrong bytes, a metadata-only change, a content-type
                     change, an extra object, a new row. Restore must repair all of them and stop
                     every service (a running redis stands in for a writer).
    empty-point      a backup of an empty bucket; objects added later are removed by the restore.
    partial-backup   a key that cannot be copied as a file (a zero-byte `partial-marker/` folder
                     marker) must fail the backup and publish no BACKUP_DIR.
  Backup metrics: BACKUP_METRICS_DIR/robot-vault-backup.prom has exactly the three
    robot_vault_backup_* lines, mode 0644, no temp file left. After fresh-storage's good backup:
    run_success 1, failure time 0. After partial-backup's failed backup: run_success 0, a failure
    time, and the success time of the good backup just before it.
  Preflight (each case first changes the destination, then restores a tampered copy of a good
      backup; the restore must exit nonzero and the database and every object, with metadata, must
      be exactly as before)
    truncated-payload, truncated-dump, missing-record, manifest-checksum (manifest changed, record
    not), malformed-manifest, duplicate-key, missing-payload, extra-payload, hash-mismatch (same
    size), path-traversal (a last entry "../escape.bin" names a real file outside objects/). The
    preflight cases below the record line are resealed:
    the record carries the tampered manifest's checksum, so only the manifest checks can refuse them.
  Transfer failure
    bad-dump         a dump that passes the checksum but pg_restore rejects: exit nonzero, services
                     stay stopped, and the restore of the good backup still works afterwards.

Artifact: a JSON report (path in argv[1]) with every case's verdict and details. Exit 0 only when
every case passes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
PASSWORD = (pathlib.Path(os.environ.get("OBJECTSTORE_ROOT_PASSWORD_FILE", REPO / "configs/objectstore-root-password.dev"))
            .read_text().strip())
ENV = dict(os.environ, OBJECTSTORE_ROOT_USER=os.environ.get("OBJECTSTORE_ROOT_USER", "robot-vault-dev-root"),
           OBJECTSTORE_ROOT_PASSWORD=PASSWORD)
LOG: list[dict] = []


def run(args: list[str], name: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=REPO, env=ENV, capture_output=True, text=True, **kwargs)
    LOG.append({"step": name, "rc": result.returncode})
    if check and result.returncode != 0:
        raise RuntimeError(f"{name} failed ({result.returncode}):\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
    return result


def probe(*args: str) -> dict:
    result = run(["docker", "compose", "run", "--rm", "--no-deps", "-T",
                  "-e", "OBJECTSTORE_ROOT_USER", "-e", "OBJECTSTORE_ROOT_PASSWORD",
                  "dev", "python3", "development/backup-restore-e2e/probe.py", *args], f"probe {args[0]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def make(target: str, folder: pathlib.Path) -> subprocess.CompletedProcess:
    # Every backup folder is directly under the run's work folder; its metrics/ holds the textfile.
    return run(["make", "--no-print-directory", target, f"BACKUP_DIR={folder}",
                f"BACKUP_METRICS_DIR={folder.parent / 'metrics'}"], f"make {target}", check=False)


METRIC_NAMES = ["robot_vault_backup_last_success_timestamp_seconds",
                "robot_vault_backup_last_failure_timestamp_seconds", "robot_vault_backup_last_run_success"]


def metrics(work: pathlib.Path) -> dict:
    """The backup metrics textfile: exactly the three lines in order, integers, ASCII, mode 0644, <= 4096 bytes."""
    path = work / "metrics" / "robot-vault-backup.prom"
    data = path.read_bytes()
    lines = [line.split(" ") for line in data.decode("ascii").splitlines()]
    assert [name for name, _ in lines] == METRIC_NAMES, lines
    assert len(data) <= 4096 and (path.stat().st_mode & 0o777) == 0o644, (len(data), oct(path.stat().st_mode))
    assert sorted(os.listdir(path.parent)) == [path.name], os.listdir(path.parent)
    return {name.removeprefix("robot_vault_backup_"): int(value) for name, value in lines}


def running() -> set[str]:
    return set(run(["docker", "compose", "ps", "--status", "running", "--services"], "ps").stdout.split())


def datastores_up() -> None:
    run(["docker", "compose", "up", "-d", "--no-deps", "--wait", "postgres", "objectstore"], "datastores up")
    run(["docker", "compose", "run", "--rm", "objectstore-access-ready"], "bucket and keys")


def fresh_state() -> None:
    """An empty bucket, a migrated database with the fixture table, the fixture objects."""
    datastores_up()
    probe("empty")
    probe("db-reset")
    run(["docker", "compose", "run", "--rm", "db-migrate"], "db-migrate")
    probe("db-seed")
    probe("plant")


def lose_store_data() -> None:
    """The store's data folder is lost: a fresh, empty store without the bucket."""
    run(["docker", "compose", "stop", "objectstore"], "stop store")
    run(["docker", "compose", "run", "--rm", "--no-deps", "--user", "0", "--entrypoint", "sh", "objectstore",
         "-c", "rm -rf /data/* /data/.[!.]*"], "wipe store data", check=False)
    run(["docker", "compose", "up", "-d", "--no-deps", "--wait", "objectstore"], "fresh store")


def differences(before: dict, after: dict) -> list[str]:
    out = []
    for part in ("db", "objects"):
        a, b = before[part], after[part]
        if part == "db":
            a, b = {**a["tables"], **a["sequences"]}, {**b["tables"], **b["sequences"]}
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                out.append(f"{part}:{key}: before={a.get(key)} after={b.get(key)}")
    return out


def copy_without_xattrs(source: pathlib.Path, target: pathlib.Path) -> None:
    shutil.copytree(source, target, copy_function=shutil.copyfile)


# --- tampering ----------------------------------------------------------------


def reseal(folder: pathlib.Path) -> None:
    record_path = folder / "recovery-point.json"
    record = json.loads(record_path.read_text())
    if (folder / "objectstore-manifest.jsonl").exists():
        manifest = (folder / "objectstore-manifest.jsonl").read_bytes()
        record["manifest"].update(size=len(manifest), sha256=hashlib.sha256(manifest).hexdigest(),
                                  objects=len(manifest.splitlines()))
    dump = (folder / "postgres.dump").read_bytes()
    record["postgres_dump"].update(size=len(dump), sha256=hashlib.sha256(dump).hexdigest())
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def manifest_lines(folder: pathlib.Path) -> list[str]:
    return (folder / "objectstore-manifest.jsonl").read_text().splitlines()


def write_manifest(folder: pathlib.Path, lines: list[str]) -> None:
    (folder / "objectstore-manifest.jsonl").write_text("".join(line + "\n" for line in lines))


def payload(folder: pathlib.Path, key: str) -> pathlib.Path:
    return folder / "objects" / key


def tamper(case: str, folder: pathlib.Path) -> None:
    small = "fixtures/meta.bin"
    if case not in ("truncated-dump", "missing-record", "bad-dump", "truncated-payload",
                    "missing-payload", "extra-payload", "hash-mismatch"):
        lines = manifest_lines(folder)
        entries = [json.loads(line) for line in lines]
    if case == "truncated-payload":
        path = payload(folder, "fixtures/recording multipart.mcap")
        os.truncate(path, path.stat().st_size // 2)
    elif case == "truncated-dump":
        path = folder / "postgres.dump"
        os.truncate(path, path.stat().st_size // 2)
    elif case == "missing-record":
        (folder / "recovery-point.json").unlink()
    elif case == "manifest-checksum":
        entries[0]["metadata"]["x-tampered"] = "1"
        write_manifest(folder, [json.dumps(entries[0], sort_keys=True, separators=(",", ":"))] + lines[1:])
    elif case == "malformed-manifest":
        write_manifest(folder, lines[:-1] + ['{"key": "fixtures/meta.bin", not json'])
        reseal(folder)
    elif case == "duplicate-key":
        write_manifest(folder, lines + [lines[0]])
        reseal(folder)
    elif case == "missing-payload":
        payload(folder, small).unlink()
    elif case == "extra-payload":
        path = payload(folder, "fixtures/not-in-manifest.bin")
        path.write_bytes(b"extra payload\n")
    elif case == "hash-mismatch":
        path = payload(folder, small)
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    elif case == "path-traversal":
        # A real file outside objects/, named by the last entry: a restore that trusts the key
        # reads it (after it has already re-uploaded the entries before it).
        data = b"outside the payload tree\n"
        (folder / "escape.bin").write_bytes(data)
        escape = dict(entries[0], key="../escape.bin", size=len(data), sha256=hashlib.sha256(data).hexdigest())
        write_manifest(folder, lines + [json.dumps(escape, sort_keys=True, separators=(",", ":"))])
        reseal(folder)
    elif case == "bad-dump":
        (folder / "postgres.dump").write_bytes(b"PGDMP not really a dump\n" * 100)
        reseal(folder)
    else:
        raise ValueError(case)


PREFLIGHT_CASES = ("truncated-payload", "truncated-dump", "missing-record", "manifest-checksum",
                   "malformed-manifest", "duplicate-key", "missing-payload", "extra-payload",
                   "hash-mismatch", "path-traversal")


# --- cases ----------------------------------------------------------------------


def case_fresh_storage(work: pathlib.Path) -> dict:
    fresh_state()
    before = probe("fingerprint")
    backup, moved = work / "fresh-backup", work / "fresh-moved"
    result = make("backup", backup)
    if result.returncode != 0:
        return {"ok": False, "why": "backup failed", "out": result.stdout[-2000:] + result.stderr[-2000:]}
    good_run = metrics(work)  # the run's first backup: no failure yet
    if not (good_run["last_run_success"] == 1 and good_run["last_success_timestamp_seconds"] > 0
            and good_run["last_failure_timestamp_seconds"] == 0):
        return {"ok": False, "why": "metrics after a good backup", "metrics": good_run}
    copy_without_xattrs(backup, moved)
    shutil.rmtree(backup)
    probe("db-reset")
    lose_store_data()
    restore = make("restore", moved)
    after = probe("fingerprint")
    diff = differences(before, after)
    return {"ok": restore.returncode == 0 and not diff, "restore_rc": restore.returncode,
            "objects": len(after["objects"]), "differences": diff[:20],
            "restore_tail": (restore.stdout + restore.stderr)[-1500:]}


def case_bucket_damaged(work: pathlib.Path) -> dict:
    fresh_state()
    before = probe("fingerprint")
    backup = work / "damaged-backup"
    result = make("backup", backup)
    if result.returncode != 0:
        return {"ok": False, "why": "backup failed", "out": result.stdout[-2000:] + result.stderr[-2000:]}
    probe("damage")
    probe("db-change", "damaged")
    damaged = probe("fingerprint")
    run(["docker", "compose", "up", "-d", "--no-deps", "redis"], "writer stand-in up")
    restore = make("restore", backup)
    still_running = sorted(running() - {"postgres", "objectstore"})
    after = probe("fingerprint")
    diff = differences(before, after)
    return {"ok": restore.returncode == 0 and not diff and not still_running and differences(before, damaged) != [],
            "restore_rc": restore.returncode, "damage_seen": differences(before, damaged)[:10],
            "differences": diff[:20], "running_after_restore": still_running,
            "restore_tail": (restore.stdout + restore.stderr)[-1500:]}


def case_empty_point(work: pathlib.Path) -> dict:
    fresh_state()
    probe("empty")
    before = probe("fingerprint")
    backup = work / "empty-backup"
    result = make("backup", backup)
    if result.returncode != 0:
        return {"ok": False, "why": "backup failed", "out": result.stdout[-2000:] + result.stderr[-2000:]}
    probe("plant")
    probe("db-change", "empty")
    restore = make("restore", backup)
    after = probe("fingerprint")
    diff = differences(before, after)
    return {"ok": restore.returncode == 0 and not diff and after["objects"] == {},
            "restore_rc": restore.returncode, "objects_after": len(after["objects"]), "differences": diff[:20],
            "restore_tail": (restore.stdout + restore.stderr)[-1500:]}


def case_partial_backup(work: pathlib.Path) -> dict:
    fresh_state()
    make("backup", work / "before-partial")  # a good backup just before, so a success time exists
    good_run = metrics(work)
    probe("marker")
    backup = work / "partial-backup"
    result = make("backup", backup)
    probe("unmark")
    failed_run = metrics(work)  # the failed run keeps the last success time
    metrics_ok = (good_run["last_run_success"] == 1 and failed_run["last_run_success"] == 0
                  and failed_run["last_success_timestamp_seconds"] == good_run["last_success_timestamp_seconds"] > 0
                  and failed_run["last_failure_timestamp_seconds"] > 0)
    leftovers = sorted(p.name for p in work.iterdir() if p.name.startswith("partial-backup"))
    return {"ok": result.returncode != 0 and leftovers == [] and metrics_ok, "backup_rc": result.returncode,
            "published": leftovers, "metrics": {"good": good_run, "failed": failed_run},
            "out": (result.stdout + result.stderr)[-1500:]}


def cases_fail_closed(work: pathlib.Path) -> dict:
    fresh_state()
    good = work / "good-backup"
    result = make("backup", good)
    if result.returncode != 0:
        return {"setup": {"ok": False, "why": "backup failed", "out": result.stdout[-2000:] + result.stderr[-2000:]}}
    results = {}

    def one(case: str) -> dict:
        datastores_up()
        probe("mutate", case)
        before = probe("fingerprint")
        folder = work / f"tampered-{case}"
        copy_without_xattrs(good, folder)
        try:
            tamper(case, folder)
        except Exception as exc:  # the recovery point has no such part (an earlier, simpler format)
            return {"ok": False, "why": f"tamper not applicable: {type(exc).__name__}: {exc}"}
        restore = make("restore", folder)
        datastores_up()
        after = probe("fingerprint")
        diff = differences(before, after)
        return {"ok": restore.returncode != 0 and not diff, "restore_rc": restore.returncode,
                "destination_changes": diff[:10], "out": (restore.stdout + restore.stderr)[-800:]}

    for case in filter(wanted, PREFLIGHT_CASES):
        try:
            results[case] = one(case)
        except Exception as exc:  # e.g. an earlier red restore broke the fixtures this case needs
            results[case] = {"ok": False, "why": f"{type(exc).__name__}: {str(exc)[-1500:]}"}
    if not wanted("bad-dump"):
        return results
    # Transfer failure after a passed preflight: writers stay stopped, the restore can run again.
    datastores_up()
    probe("mutate", "bad-dump")
    run(["docker", "compose", "up", "-d", "--no-deps", "redis"], "writer stand-in up")
    folder = work / "tampered-bad-dump"
    copy_without_xattrs(good, folder)
    try:
        tamper("bad-dump", folder)
    except Exception as exc:
        results["bad-dump"] = {"ok": False, "why": f"tamper not applicable: {type(exc).__name__}: {exc}"}
        return results
    failed = make("restore", folder)
    still_running = sorted(running() - {"postgres", "objectstore"})
    rerun = make("restore", good)
    results["bad-dump"] = {"ok": failed.returncode != 0 and not still_running and rerun.returncode == 0,
                           "restore_rc": failed.returncode, "running_after_failure": still_running,
                           "rerun_rc": rerun.returncode, "out": (failed.stdout + failed.stderr)[-800:]}
    return results


def wanted(case: str) -> bool:
    """BRE_CASES=a,b runs only those cases (a focused re-run); unset runs all of them."""
    only = os.environ.get("BRE_CASES")
    return not only or case in only.split(",")


def main() -> int:
    report_path = pathlib.Path(sys.argv[1]).resolve()
    work = pathlib.Path(tempfile.mkdtemp(prefix="bre-", dir=os.environ.get("BRE_WORK_DIR")))
    (work / "metrics").mkdir()
    report: dict = {
        "test": "development/backup-restore-e2e/e2e.py",
        "started_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip(),
        "tool_sha256": hashlib.sha256((REPO / "tools/robot_vault_backup.py").read_bytes()).hexdigest(),
        "cases": {},
    }
    try:
        for name, function in (("fresh-storage", case_fresh_storage), ("bucket-damaged", case_bucket_damaged),
                               ("empty-point", case_empty_point), ("partial-backup", case_partial_backup)):
            if not wanted(name):
                continue
            try:
                report["cases"][name] = function(work)
            except Exception as exc:
                report["cases"][name] = {"ok": False, "why": f"{type(exc).__name__}: {str(exc)[-2000:]}"}
            print(f"{name}: {'PASS' if report['cases'][name]['ok'] else 'FAIL'}", flush=True)
        try:
            for name, verdict in cases_fail_closed(work).items():
                report["cases"][name] = verdict
                print(f"{name}: {'PASS' if verdict['ok'] else 'FAIL'}", flush=True)
        except Exception as exc:
            report["cases"]["fail-closed-setup"] = {"ok": False, "why": f"{type(exc).__name__}: {str(exc)[-2000:]}"}
            print("fail-closed-setup: FAIL", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        report["steps"] = LOG
        report["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report["result"] = "PASS" if report["cases"] and all(c.get("ok") for c in report["cases"].values()) else "FAIL"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"report: {report_path} ({report['result']})")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
