"""Offline proof that the conformance suite is a failing gate.

No store and no Docker. `conformance.py` runs against a closed port, so every probe fails.
The gate must then exit nonzero, and it must also exit nonzero when a service key is missing,
before it sends any request. A suite that records failures in its JSON but returns 0, or that turns a missing key's probes
into "n/a" or a silent fall back to the root key, must fail this test.

The verdict logic (`gate.py`) is covered by `test_gate_verdict`: one failed probe and one
missing probe each give a nonzero exit.

Run: python -m pytest development/objectstore-conformance/test_conformance_gate.py -q
(needs boto3 and requests; the runner image has them).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SERVICES = ("ingest", "processor", "exporter", "query", "gc")


def env_for(missing: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("S3_", "ROOT_", "RESULTS"))}
    env.update(S3_ENDPOINT="http://127.0.0.1:9", S3_PUBLIC_ENDPOINT="http://127.0.0.1:9", HEALTH_URL="http://127.0.0.1:9/health",
               S3_BUCKET="rv-offline", S3_REGION="us-east-1", STORE="offline",
               ROOT_ACCESS_KEY="root-access", ROOT_SECRET_KEY="root-secret-0000000001")
    for s in SERVICES:
        if s == missing:
            continue
        # Both spellings (<SVC>_ACCESS_KEY and <SVC>_S3_ACCESS_KEY), so a missing key is missing under either name.
        for a, b in (("ACCESS_KEY", f"robot-vault-{s}"), ("SECRET_KEY", f"secret-{s}-0000000001")):
            env[f"{s.upper()}_{a}"] = b
            env[f"{s.upper()}_S3_{a}"] = b
    return env


def run_suite(tmp_path: Path, missing: str | None = None) -> subprocess.CompletedProcess:
    env = env_for(missing)
    env["RESULTS"] = str(tmp_path / "conformance.json")
    env["REPO"] = str(HERE.parents[1])
    return subprocess.run([sys.executable, str(HERE / "conformance.py")], env=env, capture_output=True, text=True,
                          timeout=300)


def test_every_probe_failing_is_red(tmp_path):
    p = run_suite(tmp_path)
    assert p.returncode != 0, "the suite exited 0 although the store was unreachable and every probe failed"


@pytest.mark.parametrize("service", SERVICES)
def test_missing_service_key_is_red_and_named(tmp_path, service):
    p = run_suite(tmp_path, missing=service)
    assert p.returncode != 0, f"the suite exited 0 although the {service} key is missing"
    assert f"{service.upper()}_S3_ACCESS_KEY" in p.stdout + p.stderr, "the message does not name the missing key"
    assert not (tmp_path / "conformance.json").exists(), "the suite ran probes before it checked the keys"


def test_gate_verdict(tmp_path):
    gate = HERE / "gate.py"
    assert gate.exists(), "gate.py is missing"

    def verdict(**overrides) -> int:
        for name, body in {**ok_artifacts(), **overrides}.items():
            if body is None:
                (tmp_path / name).unlink(missing_ok=True)
            else:
                (tmp_path / name).write_text(body if isinstance(body, str) else json.dumps(body))
        return subprocess.run([sys.executable, str(gate), str(tmp_path)], capture_output=True, text=True).returncode

    assert verdict() == 0, "a complete passing run must be green"
    bad = ok_artifacts()["conformance.json"]
    bad["results"][3]["status"] = "fail"
    assert verdict(**{"conformance.json": bad}) != 0, "one failed conformance probe must be red"
    na = ok_artifacts()["conformance.json"]
    na["results"][3]["status"] = "n/a"
    assert verdict(**{"conformance.json": na}) != 0, "a skipped probe counts as missing"
    assert verdict(**{"crash-verify.json": None}) != 0, "a missing crash result must be red"
    assert verdict(**{"iam.json": {"probes": 290, "status": "fail"}}) != 0, "a failed IAM probe must be red"
    steps = ok_artifacts()["steps.txt"].replace("expiry-green pass", "expiry-green fail")
    assert verdict(**{"steps.txt": steps}) != 0, "a failed step must be red"
    assert verdict(**{"steps.txt": "keys pass\n"}) != 0, "steps that never ran must be red"
    assert verdict(**{"expiry-red.json": {"status": "pass"}}) != 0, "a short expiry that keeps the upload must be red"


def ok_artifacts() -> dict:
    sys.path.insert(0, str(HERE))
    import gate  # noqa: PLC0415 - imported late so a missing gate.py is one clear failure

    results = [{"feature": f, "status": "pass", "detail": "", "seconds": 0} for f in gate.REQUIRED_FEATURES]
    out = {
        "conformance.json": {"store": "t", "results": results},
        "iam.json": {"probes": 290, "over_permit": [], "under_permit": [], "errors": [], "status": "pass"},
        "argv.json": {"status": "pass", "checks": {}},
        "crash-verify.json": {"status": "pass", "acknowledged": 200, "problems": []},
        "expiry-red.json": {"status": "fail"},
        "expiry-green.json": {"status": "pass"},
        "contract-probe.json": [{"check": f"c{i}", "pass": True} for i in range(gate.CONTRACT_CHECKS)],
        "steps.txt": "".join(f"{s} pass\n" for s in gate.REQUIRED_STEPS),
    }
    return out
