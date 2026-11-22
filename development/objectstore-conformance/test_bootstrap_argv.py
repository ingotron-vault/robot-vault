"""Proof that bootstrap.py keeps every secret out of argv (and out of its output).

Runs inside the one-shot container. While bootstrap.py runs, a sampler reads
/proc/*/cmdline of every process in the container in a tight loop. The test fails if any
sampled command line contains a secret, if the sampler saw the bootstrap process too few
times to mean anything, or if a secret appears on stdout or stderr.

Control (red): a process started with a secret in argv must be caught by the same sampler,
so a sampler that cannot see command lines cannot pass the test.

Secrets are passed both ways the bootstrap supports: root password and three service secrets
as <NAME>_FILE, two service secrets as plain environment variables. The bootstrap runs twice
(idempotence), then once with a short secret, which must be rejected without echoing it.
Writes <out>/argv.json; exit 1 on any failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOTSTRAP = HERE.parent / "objectstore" / "bootstrap.py"  # the product program, not a copy
from suite_env import SERVICES, require_env  # noqa: E402


def secrets_env(tmp: Path) -> tuple[dict, list[str]]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_SECRET_KEY") and k not in ("ROOT_SECRET_KEY", "OBJECTSTORE_ROOT_PASSWORD")}
    secret_values = [os.environ["ROOT_SECRET_KEY"]] + [os.environ[f"{s.upper()}_S3_SECRET_KEY"] for s in SERVICES]
    env["OBJECTSTORE_ROOT_USER"] = os.environ["ROOT_ACCESS_KEY"]
    as_file = {"OBJECTSTORE_ROOT_PASSWORD": os.environ["ROOT_SECRET_KEY"]}
    for s in SERVICES[:3]:
        as_file[f"{s.upper()}_S3_SECRET_KEY"] = os.environ[f"{s.upper()}_S3_SECRET_KEY"]
    for s in SERVICES[3:]:
        env[f"{s.upper()}_S3_SECRET_KEY"] = os.environ[f"{s.upper()}_S3_SECRET_KEY"]
    for name, val in as_file.items():
        p = tmp / name.lower()
        p.write_text(val + "\n", encoding="utf-8")
        p.chmod(0o600)
        env[f"{name}_FILE"] = str(p)
    return env, secret_values


def cmdlines() -> dict[int, str]:
    seen = {}
    me = os.getpid()
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) != me:
            try:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    seen[int(d)] = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                pass
    return seen


def sampled_run(argv: list[str], env: dict, secrets: list[str]) -> dict:
    p = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    samples = target_seen = 0
    leaks = set()
    while p.poll() is None:
        lines = cmdlines()
        samples += 1
        if p.pid in lines and lines[p.pid]:
            target_seen += 1
        for pid, line in lines.items():
            for s in secrets:
                if s in line:
                    leaks.add(f"pid {pid}: secret #{secrets.index(s)} in argv")
    out, err = p.communicate()
    text = (out + err).decode("utf-8", "replace")
    leaks |= {f"output: secret #{secrets.index(s)}" for s in secrets if s in text}
    return {"argv": argv[1:], "rc": p.returncode, "samples": samples, "target_seen": target_seen,
            "leaks": sorted(leaks), "output": text.strip()[-300:]}


def main(out: str) -> int:
    require_env()
    with tempfile.TemporaryDirectory() as d:
        env, secrets = secrets_env(Path(d))
        control = sampled_run(["sh", "-c", 'sleep 1 # "$0"', secrets[1]], env, secrets)
        runs = [sampled_run([sys.executable, str(BOOTSTRAP)], env, secrets) for _ in range(2)]
        bad_env = dict(env, QUERY_S3_SECRET_KEY="short-secret-1")
        rejected = sampled_run([sys.executable, str(BOOTSTRAP)], bad_env, secrets + ["short-secret-1"])
    checks = {
        "control_caught": bool(control["leaks"]),
        "bootstrap_runs_ok": all(r["rc"] == 0 for r in runs),
        "bootstrap_sampled": all(r["target_seen"] >= 20 for r in runs),
        "no_leak_in_bootstrap": not any(r["leaks"] for r in runs),
        "short_secret_rejected": rejected["rc"] == 1 and "must be at least 16 characters" in rejected["output"],
        "no_leak_on_reject": not rejected["leaks"],
    }
    result = {"checks": checks, "control": control, "runs": runs, "rejected": rejected,
              "status": "pass" if all(checks.values()) else "fail"}
    with open(f"{out}/argv.json", "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps({"argv": result["status"], **checks,
                      "samples": [r["samples"] for r in runs], "target_seen": [r["target_seen"] for r in runs]}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
