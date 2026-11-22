"""The operator commands: `status` and `credential rotate`.

Separate from test_agent_runtime.py because these drive the whole `ingotron-agent`
surface against a relocated filesystem, rather than testing one function.

`paths` reads INGOTRON_ROOT_PREFIX at import time, so every test that needs a
sandbox reloads the module after setting it. Doing that explicitly, rather than
hiding it in a fixture, keeps the import-time coupling visible: a future change
that reads the prefix later would make these tests lie.
"""

import importlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_PACKAGING_ROOT = _THIS_DIR.parent
_REPO_ROOT = _PACKAGING_ROOT.parent.parent
_AGENT_LIB = _PACKAGING_ROOT / "agent" / "lib"
_COMMON_CONFIG_SRC = _REPO_ROOT / "phases" / "common-config" / "src"

for _path in (_AGENT_LIB, _COMMON_CONFIG_SRC):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)


#: A credential of exactly the per-robot token shape: `ignr1.` + 52 Crockford chars.
SENTINEL_TOKEN = "ignr1." + "SENTINEL7T3DZ8H1RN6VBX0S3EYA2GJT4KQ29WMF7T3DZ8H1RN6V"


def _sandbox(tmp_path, monkeypatch, *modules):
    """Relocate /etc/ingotron and /var/lib/ingotron under tmp_path.

    Returns the reloaded `paths` module. Every module named in `modules` is
    reloaded too, because they captured `paths`' constants at import time.
    """
    monkeypatch.setenv("INGOTRON_ROOT_PREFIX", str(tmp_path))
    import ingotron_agent.paths as paths_mod

    importlib.reload(paths_mod)
    for name in modules:
        importlib.reload(importlib.import_module(name))
    return paths_mod


def _write_enrolled_state(paths_mod, token=SENTINEL_TOKEN, self_rotation=True):
    paths_mod.CRED_DIR.mkdir(parents=True, exist_ok=True)
    paths_mod.TOKEN_FILE.write_text(token, encoding="ascii")
    os.chmod(str(paths_mod.TOKEN_FILE), 0o600)
    paths_mod.ENROLLMENT_STATE.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.ENROLLMENT_STATE.write_text(
        json.dumps(
            {
                "robot_id": "rover-7",
                "credential_fingerprint": "a3f01c9e2b44",
                "ingest_endpoint": "rover-vault.local:50051",
                "self_rotation_enabled": self_rotation,
            }
        ),
        encoding="utf-8",
    )


def _write_vault_config(tmp_path, paths_mod, recordings):
    config = tmp_path / "vault.json"
    config.write_text(
        json.dumps(
            {
                "robot_id": "rover-7",
                "paths": {"robot_root": str(recordings)},
                "server": {"host": "rover-vault.local", "port": 50051},
                "tls": {"enabled": True, "mtls": False, "ca_path": str(tmp_path / "ca.crt")},
                "auth": {"token_file": str(paths_mod.TOKEN_FILE)},
            }
        ),
        encoding="utf-8",
    )
    return config


# --- `status` -----------------------------------------------------------


def test_status_never_prints_the_token_or_a_52_char_base32_run(tmp_path, monkeypatch, capsys):
    """The full `status` output contains neither the token nor any
    52-character base32 run. The non-secret fingerprint IS expected
    to appear -- that is what it is for.
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.status")
    import ingotron_agent.status as status_mod

    _write_enrolled_state(paths_mod)

    assert status_mod.main(tmp_path / "no-such-vault.json") == 0
    out = capsys.readouterr().out

    assert SENTINEL_TOKEN not in out, "status printed the credential"
    assert "ignr1." not in out, "status printed something credential-shaped"
    runs = re.findall(r"[0-9A-HJKMNP-TV-Z]{52}", out)
    assert runs == [], f"status printed a 52-character base32 run: {runs}"
    assert "a3f01c9e2b44" in out, "the non-secret fingerprint should be shown"


def test_status_creates_zero_new_manifest_files(tmp_path, monkeypatch):
    """Running `status` must create ZERO new manifest files.

    One could expect `UploadQueue.describe()` to create missing manifests as a
    side effect, which would need a no-write mode. Measured, that is not
    true -- `load_or_create_manifest` returns an in-memory dict and only
    `save_manifest` persists -- so no no-write mode was added. This test is what turns "it doesn't" from a belief into a gate: if a
    future change makes describe() persist, it fires here.
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.status")
    import ingotron_agent.status as status_mod

    _write_enrolled_state(paths_mod)

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    # TWELVE files, not two, and that number is the point: `describe()` returns
    # `"pending": pending[:10]` -- a sample capped at ten -- so a status
    # implementation that sums sizes over that list reports the first ten files
    # of a backlog as if they were the whole queue. With two files such a bug
    # is invisible.
    file_count = 12
    for index in range(file_count):
        (recordings / f"rec-{index:02d}.mcap").write_bytes(b"\x00" * 64)
    config = _write_vault_config(tmp_path, paths_mod, recordings)

    before = sorted(p.name for p in recordings.rglob("*"))
    report = status_mod.build(config)
    after = sorted(p.name for p in recordings.rglob("*"))

    assert after == before, f"status created files: {sorted(set(after) - set(before))}"
    queue = report["queue"]
    assert queue.get("error") is None, queue
    assert queue["total_files"] == file_count
    assert queue["pending_files"] == file_count, "the pending count was truncated"
    assert queue["bytes_pending"] == 64 * file_count, "bytes pending was truncated"
    # `status` must print "queue depth BY MANIFEST STATUS", which describe() does not
    # provide at all -- an empty breakdown would silently satisfy a weaker test.
    assert queue["by_status"], "no per-status breakdown was produced"
    assert sum(queue["by_status"].values()) == file_count


def test_status_reports_a_swapped_ca_as_a_mismatch(tmp_path, monkeypatch):
    """The installer stores the full SPKI hash so the agent DETECTS a swapped CA.

    Recording it and never comparing it would be pure ceremony, so this asserts
    the comparison happens and reports the disagreement.
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.status")
    import ingotron_agent.status as status_mod

    _write_enrolled_state(paths_mod)

    import subprocess

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=vault", "-addext", "basicConstraints=critical,CA:TRUE",
            "-keyout", str(tmp_path / "ca.key"), "-out", str(paths_mod.CA_CERT),
        ],
        check=True,
        capture_output=True,
    )
    paths_mod.CA_SPKI_SHA256.write_text("00" * 32 + "\n", encoding="ascii")

    state = status_mod.ca_pin_state()
    assert state["match"] is False
    assert "does not match" in state["error"]
    assert len(state["observed_spki_sha256"]) == 64
    assert state["pin"] is not None and len(state["pin"]) == 26


def test_status_never_uses_healthcheck_as_a_credential_probe():
    """HealthCheck is the one data-plane RPC that does not verify the credential.

    So the rule is: anything that must
    demonstrate a credential works -- `ingotron-agent status`, the compromise
    drill's "confirm the robot is cut off" step, any future connectivity
    diagnostic -- uses ListUploads, GetUploadStatus or Upload, never
    HealthCheck, and never opens a channel to it by any route.

    Measured live by tests/live_enrollment_gate.sh: `health_check()` never
    calls `resolve_request_identity`, and the interceptor rejects only a
    MALFORMED bearer, so a well-formed but never-issued, revoked or
    decommissioned `ignr1.` token reaches a handler that ignores it -- a
    FABRICATED token returns `{"status":"healthy"}`.

    The failure this prevents is a drill that reports a revoked robot as
    reachable-and-authenticated, during an incident, which is the one moment
    the answer has to be true.

    A source-level assertion on purpose: the risk is a future contributor
    "improving" status by adding a probe, and the reason not to is not
    discoverable from the function's signature.
    """
    import ast

    source = (_AGENT_LIB / "ingotron_agent" / "status.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Parsed, not grepped. A text search here would match the docstring that
    # EXPLAINS why health_check must not be called, and "delete the explanation
    # to make the test pass" is the wrong direction. The AST sees calls only.
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg == "probe_server":
            assert not (
                isinstance(node.value, ast.Constant) and node.value.value is True
            ), "status.py passes probe_server=True"

    assert "health_check" not in referenced, (
        "status.py calls health_check; that RPC does not authenticate the credential "
        "(HealthCheck does not verify credentials)"
    )
    # And it must not open a channel at all: `status` is not a way to make a
    # revoked robot hammer the vault on a loop.
    for forbidden in ("secure_channel", "insecure_channel", "_new_transport"):
        assert forbidden not in referenced, f"status.py opens a channel via {forbidden}"
    # `describe()` is the truncating sampler `status` must not use.
    assert "describe" not in referenced, (
        "status.py calls UploadQueue.describe(), which returns pending[:10] -- a "
        "sample capped at ten with no per-status breakdown"
    )


# --- `credential rotate` -------------------------------------------------


def test_credential_rotate_is_a_clean_no_op_when_the_vault_disabled_it(
    tmp_path, monkeypatch, capsys
):
    """`self_rotation_enabled=false` is a CONFIGURED state, not an error.

    robot.sh installs no rotate timer in that case, so this path is only
    reached by an operator running the command by hand, and the plain
    "disabled by the vault" message is what they should see. Exiting
    non-zero would make a correctly configured vault look broken.
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.cli")
    import ingotron_agent.cli as cli_mod

    _write_enrolled_state(paths_mod, self_rotation=False)

    assert cli_mod.main(["credential", "rotate"]) == 0
    captured = capsys.readouterr()
    assert "disabled by the vault" in captured.out
    assert captured.err == ""


def test_credential_rotate_reports_the_missing_rpc_when_the_vault_enabled_it(
    tmp_path, monkeypatch, capsys
):
    """The other half: the flag says yes and the wire has no RotateCredential.

    That combination is a real problem an operator must see, so it exits
    non-zero and names the gap rather than logging a shrug every week.
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.cli")
    import ingotron_agent.cli as cli_mod

    _write_enrolled_state(paths_mod, self_rotation=True)

    assert cli_mod.main(["credential", "rotate"]) == cli_mod.EXIT_UNSUPPORTED
    captured = capsys.readouterr()
    assert "RotateCredential" in captured.err
    assert SENTINEL_TOKEN not in captured.err + captured.out


def test_credential_rotate_refuses_when_the_robot_is_not_enrolled(tmp_path, monkeypatch, capsys):
    _sandbox(tmp_path, monkeypatch, "ingotron_agent.cli")
    import ingotron_agent.cli as cli_mod

    assert cli_mod.main(["credential", "rotate"]) == cli_mod.EXIT_ERROR
    assert "not enrolled" in capsys.readouterr().err


# --- `enroll` refusals ------------------------------------------------------


def test_enroll_refuses_over_an_existing_token_without_force(tmp_path, monkeypatch):
    """`enroll` refuses over an existing enrollment without --force.

    The refusal must happen BEFORE the RPC, or a stray re-run burns a fresh
    enrollment code on a robot that did not need one -- and a burnt code with a
    lost response is the one race the enrollment design cannot remove.
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    _write_enrolled_state(paths_mod)

    reached = {"exchange": False}

    def _boom(**_kwargs):
        reached["exchange"] = True
        raise AssertionError("exchange_code must not be reached")

    monkeypatch.setattr(enroll_mod, "exchange_code", _boom)

    with pytest.raises(enroll_mod.EnrollmentError) as exc:
        enroll_mod.enroll(
            vault_url="https://vault.test:8003",
            code="ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
            ca_path=tmp_path / "ca.crt",
            config_path=tmp_path / "vault.json",
            token_file=paths_mod.TOKEN_FILE,
            recordings_root=str(tmp_path / "recordings"),
        )

    assert "already enrolled" in str(exc.value)
    assert reached["exchange"] is False, "the code was sent to the vault anyway"


def test_resume_finishes_an_interrupted_enrolment_without_a_code(tmp_path, monkeypatch):
    """A re-run after a step-7 failure finishes the enrolment without a code.

    robot.sh step 7 stores the credential BEFORE checking that the recordings tree
    is writable, "so a re-run does not need a new code". That promise only
    holds if something can finish the install from what is already on disk: the
    code was one-use and was spent the instant the server committed, so an
    exchange is not available and `--force` would need a fresh code.

    Without this path a chmod failure on the recordings tree would brick the
    robot -- token present, no units, the one-liner refusing over the token,
    `--force` needing a code that no longer exists -- even though the script
    says "re-running this command does NOT need a new enrollment code".
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    _write_enrolled_state(paths_mod, self_rotation=False)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    config = tmp_path / "vault.json"
    result = tmp_path / "enroll.result"

    def _boom(**_kwargs):
        raise AssertionError("resume must not perform an exchange")

    monkeypatch.setattr(enroll_mod, "exchange_code", _boom)

    state = enroll_mod.resume(
        config_path=config,
        token_file=paths_mod.TOKEN_FILE,
        ca_path=tmp_path / "ca.crt",
        recordings_root=str(recordings),
        result_file=result,
    )

    assert state["robot_id"] == "rover-7"
    # vault.json was rebuilt from the recorded facts, pointing at the SAME
    # credential -- the one the vault already issued.
    written = json.loads(config.read_text(encoding="utf-8"))
    assert written["auth"]["token_file"] == str(paths_mod.TOKEN_FILE)
    assert written["server"]["host"] == "rover-vault.local"
    assert written["tls"]["enabled"] is True
    assert written["paths"]["robot_root"] == str(recordings)
    # ...and the result file robot.sh reads carries the rotation flag, so the
    # resumed install makes the same timer decision as a fresh one.
    assert "self_rotation_enabled=false" in result.read_text(encoding="utf-8")
    # The credential is untouched.
    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == SENTINEL_TOKEN
    assert oct(paths_mod.TOKEN_FILE.stat().st_mode & 0o777) == "0o600"


def test_resume_refuses_when_there_is_no_stored_credential(tmp_path, monkeypatch):
    """Resume is for finishing an exchange that happened, not for skipping one."""
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    with pytest.raises(enroll_mod.EnrollmentError) as exc:
        enroll_mod.resume(
            config_path=tmp_path / "vault.json",
            token_file=paths_mod.TOKEN_FILE,
            ca_path=tmp_path / "ca.crt",
            recordings_root=str(tmp_path),
        )
    assert "has not completed an exchange" in str(exc.value)


def test_resume_says_what_to_do_when_the_recorded_facts_are_gone(tmp_path, monkeypatch):
    """A token with no enrollment.json cannot be resumed -- and the operator
    must be told the credential is still valid and how to replace it, rather
    than being left to guess."""
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    paths_mod.CRED_DIR.mkdir(parents=True, exist_ok=True)
    paths_mod.TOKEN_FILE.write_text(SENTINEL_TOKEN, encoding="ascii")

    with pytest.raises(enroll_mod.EnrollmentError) as exc:
        enroll_mod.resume(
            config_path=tmp_path / "vault.json",
            token_file=paths_mod.TOKEN_FILE,
            ca_path=tmp_path / "ca.crt",
            recordings_root=str(tmp_path),
        )
    message = str(exc.value)
    assert "still valid" in message
    assert "reenroll" in message


def test_enroll_validates_the_code_before_opening_anything(tmp_path, monkeypatch):
    """A malformed code must not reach the network from the agent either.

    robot.sh validates first, but `ingotron-agent enroll` is a supported direct
    entry point and must not be the weaker of the two.
    """
    _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    def _boom(**_kwargs):
        raise AssertionError("exchange_code must not be reached")

    monkeypatch.setattr(enroll_mod, "exchange_code", _boom)

    with pytest.raises(enroll_mod.EnrollmentError):
        enroll_mod.enroll(
            vault_url="https://vault.test:8003",
            code="ign2.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
            ca_path=tmp_path / "ca.crt",
            config_path=tmp_path / "vault.json",
            token_file=tmp_path / "robot.token",
            recordings_root=str(tmp_path / "recordings"),
        )


def test_validate_credential_enforces_the_token_shape():
    import ingotron_agent.enroll as enroll_mod

    enroll_mod.validate_credential("ignr1." + "A" * 52)
    for bad in ("ign1." + "A" * 52, "ignr1." + "A" * 51, "ignr1." + "A" * 53, ""):
        with pytest.raises(enroll_mod.EnrollmentError):
            enroll_mod.validate_credential(bad)


def test_enrollment_result_file_carries_no_secret(tmp_path, monkeypatch):
    """robot.sh reads this file to decide whether to install the rotate timer.

    It must carry the fingerprint and the flag and NOT the credential: robot.sh
    writes it into a temp dir, and a secret there would outlive the exchange.
    """
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    result = tmp_path / "enroll.result"
    enroll_mod._write_result_file(
        result,
        {
            "robot_id": "rover-7",
            "credential_fingerprint": "a3f01c9e2b44",
            "ingest_endpoint": "rover-vault.local:50051",
            "self_rotation_enabled": False,
        },
    )
    text = result.read_text(encoding="utf-8")
    assert "self_rotation_enabled=false" in text
    assert "robot_id=rover-7" in text
    assert "ignr1." not in text
    assert oct(os.stat(result).st_mode & 0o777) == "0o600"
    assert paths_mod is not None


# --- `reenroll` ------------------------------------------------------------


def test_reenroll_preserves_the_installed_recording_root(tmp_path, monkeypatch, capsys):
    """The UI's re-enrollment command must not reset a working robot to /data.

    Re-enrollment intentionally replaces the credential and server-provided
    tuning. Its local recording path is a machine setting, so the dedicated
    command reads it from the installed config unless an operator overrides it.
    """
    paths_mod = _sandbox(
        tmp_path,
        monkeypatch,
        "ingotron_agent.enroll",
        "ingotron_agent.cli",
    )
    import ingotron_agent.cli as cli_mod
    import ingotron_agent.enroll as enroll_mod

    _write_enrolled_state(paths_mod)
    recordings = tmp_path / "mounted_recordings"
    recordings.mkdir()
    source_config = _write_vault_config(tmp_path, paths_mod, recordings)
    paths_mod.VAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.VAULT_CONFIG.write_bytes(source_config.read_bytes())
    replacement = "ignr1." + "R" * 52

    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        enroll_mod,
        "exchange_code",
        lambda **_kwargs: {
            "robot_id": "rover-7",
            "credential": replacement,
            "credential_fingerprint": "replacement-credential",
            "ingest_endpoint": "new-vault.local:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {},
        },
    )

    assert (
        cli_mod.main(
            [
                "reenroll",
                "--vault",
                "https://new-vault.local:8003",
                "--code",
                "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
            ]
        )
        == cli_mod.EXIT_OK
    )

    written = json.loads(paths_mod.VAULT_CONFIG.read_text(encoding="utf-8"))
    assert written["paths"]["robot_root"] == str(recordings)
    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == replacement
    assert replacement not in capsys.readouterr().out


def test_reenroll_refuses_a_host_without_an_installed_credential(tmp_path, monkeypatch, capsys):
    _sandbox(tmp_path, monkeypatch, "ingotron_agent.cli")
    import ingotron_agent.cli as cli_mod

    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0)
    assert (
        cli_mod.main(
            [
                "reenroll",
                "--vault",
                "https://vault.test:8003",
                "--code",
                "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
            ]
        )
        == cli_mod.EXIT_ERROR
    )
    assert "first-install command" in capsys.readouterr().err


def test_reenroll_retries_a_post_exchange_config_failure_without_spending_a_new_code(
    tmp_path, monkeypatch
):
    paths_mod = _sandbox(
        tmp_path,
        monkeypatch,
        "ingotron_agent.enroll",
        "ingotron_agent.cli",
    )
    import ingotron_agent.cli as cli_mod
    import ingotron_agent.enroll as enroll_mod

    _write_enrolled_state(paths_mod)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    source_config = _write_vault_config(tmp_path, paths_mod, recordings)
    paths_mod.VAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.VAULT_CONFIG.write_bytes(source_config.read_bytes())
    exchanges = {"count": 0}

    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0)

    def _exchange(**_kwargs):
        exchanges["count"] += 1
        return {
            "robot_id": "rover-7",
            "credential": "ignr1." + "N" * 52,
            "credential_fingerprint": "new-credential",
            "ingest_endpoint": "vault.test:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {},
        }

    monkeypatch.setattr(enroll_mod, "exchange_code", _exchange)
    original_write = enroll_mod.write_vault_config
    monkeypatch.setattr(enroll_mod, "write_vault_config", lambda *_args: (_ for _ in ()).throw(OSError(28, "full")))
    command = [
        "reenroll",
        "--vault",
        "https://vault.test:8003",
        "--code",
        "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
    ]

    assert cli_mod.main(command) == cli_mod.EXIT_ERROR
    assert paths_mod.ENROLLMENT_PENDING.is_file()
    monkeypatch.setattr(enroll_mod, "write_vault_config", original_write)

    assert cli_mod.main(command) == cli_mod.EXIT_OK
    assert exchanges["count"] == 1
    assert not paths_mod.ENROLLMENT_PENDING.exists()


def test_reenroll_recovers_when_state_write_fails_after_the_token_replacement(
    tmp_path, monkeypatch, capsys
):
    """The pending record, not enrollment.json, is the post-exchange authority.

    A failed state write must not leave `reenroll-pending` pointing at a
    missing enrollment.json. The first call has already spent its one-use code
    and replaced the token when the public state write fails. A second call must
    reconstruct state and config without performing another exchange.
    """
    paths_mod = _sandbox(
        tmp_path,
        monkeypatch,
        "ingotron_agent.enroll",
        "ingotron_agent.cli",
    )
    import ingotron_agent.cli as cli_mod
    import ingotron_agent.enroll as enroll_mod

    _write_enrolled_state(paths_mod)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    source_config = _write_vault_config(tmp_path, paths_mod, recordings)
    paths_mod.VAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.VAULT_CONFIG.write_bytes(source_config.read_bytes())
    replacement = "ignr1." + "W" * 52
    exchanges = {"count": 0}

    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0)

    def _exchange(**_kwargs):
        exchanges["count"] += 1
        return {
            "robot_id": "rover-7",
            "credential": replacement,
            "credential_fingerprint": "replacement-credential",
            "ingest_endpoint": "new-vault.test:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {},
        }

    monkeypatch.setattr(enroll_mod, "exchange_code", _exchange)
    original_write_state = enroll_mod._write_state
    monkeypatch.setattr(
        enroll_mod,
        "_write_state",
        lambda _state: (_ for _ in ()).throw(OSError(28, "full")),
    )
    command = [
        "reenroll",
        "--vault",
        "https://new-vault.test:8003",
        "--code",
        "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
    ]

    assert cli_mod.main(command) == cli_mod.EXIT_ERROR
    pending = paths_mod.ENROLLMENT_PENDING
    assert pending.is_file()
    assert oct(pending.stat().st_mode & 0o777) == "0o600"
    assert oct(pending.parent.stat().st_mode & 0o777) == "0o700"
    # The root-only recovery record contains the new credential, but operator
    # output and public enrollment.json do not.
    output = capsys.readouterr()
    assert replacement not in output.out + output.err
    assert replacement not in paths_mod.ENROLLMENT_STATE.read_text(encoding="utf-8")

    monkeypatch.setattr(enroll_mod, "_write_state", original_write_state)
    assert cli_mod.main(command) == cli_mod.EXIT_OK
    assert exchanges["count"] == 1
    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == replacement
    assert not pending.exists()


def test_reenroll_recovers_if_token_replacement_never_commits(tmp_path, monkeypatch):
    """A crash after pending-record fsync but before token rename is recoverable."""
    paths_mod = _sandbox(
        tmp_path,
        monkeypatch,
        "ingotron_agent.enroll",
        "ingotron_agent.cli",
    )
    import ingotron_agent.cli as cli_mod
    import ingotron_agent.enroll as enroll_mod

    _write_enrolled_state(paths_mod)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    source_config = _write_vault_config(tmp_path, paths_mod, recordings)
    paths_mod.VAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.VAULT_CONFIG.write_bytes(source_config.read_bytes())
    replacement = "ignr1." + "X" * 52
    exchanges = {"count": 0}

    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0)

    def _exchange(**_kwargs):
        exchanges["count"] += 1
        return {
            "robot_id": "rover-7",
            "credential": replacement,
            "credential_fingerprint": "replacement-credential",
            "ingest_endpoint": "new-vault.test:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {},
        }

    monkeypatch.setattr(enroll_mod, "exchange_code", _exchange)
    original_write_secret = enroll_mod.secretstore.write_secret
    monkeypatch.setattr(
        enroll_mod.secretstore,
        "write_secret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(28, "full")),
    )
    command = [
        "reenroll",
        "--vault",
        "https://new-vault.test:8003",
        "--code",
        "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
    ]

    assert cli_mod.main(command) == cli_mod.EXIT_ERROR
    assert paths_mod.ENROLLMENT_PENDING.is_file()
    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == SENTINEL_TOKEN

    monkeypatch.setattr(enroll_mod.secretstore, "write_secret", original_write_secret)
    assert cli_mod.main(command) == cli_mod.EXIT_OK
    assert exchanges["count"] == 1
    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == replacement
    assert not paths_mod.ENROLLMENT_PENDING.exists()


def test_reenroll_refuses_a_tampered_pending_credential_fingerprint(tmp_path, monkeypatch, capsys):
    """A marker is trusted only after its credential and digest agree."""
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    replacement = "ignr1." + "Y" * 52
    state = {
        "robot_id": "rover-7",
        "credential_fingerprint": "replacement-credential",
        "ingest_endpoint": "new-vault.test:50051",
        "vault_url": "https://new-vault.test:8003",
        "enrolled_at": "2026-09-03T00:00:00Z",
        "self_rotation_enabled": False,
        "agent_version": "0.9.3",
        "recommended": {},
        "recordings_root": str(tmp_path / "recordings"),
    }
    (tmp_path / "recordings").mkdir()
    enroll_mod._write_recovery_marker(paths_mod.ENROLLMENT_PENDING, state, replacement)
    marker = json.loads(paths_mod.ENROLLMENT_PENDING.read_text(encoding="utf-8"))
    marker["credential_sha256"] = "0" * 64
    paths_mod.ENROLLMENT_PENDING.write_text(json.dumps(marker), encoding="utf-8")
    os.chmod(paths_mod.ENROLLMENT_PENDING, 0o600)

    with pytest.raises(enroll_mod.EnrollmentError, match="fingerprint does not match"):
        enroll_mod.resume(
            config_path=paths_mod.VAULT_CONFIG,
            token_file=paths_mod.TOKEN_FILE,
            ca_path=paths_mod.CA_CERT,
            recordings_root=None,
        )
    output = capsys.readouterr()
    assert replacement not in output.out + output.err


def test_purge_shreds_an_interrupted_enrollment_credential_copy(tmp_path, monkeypatch):
    """The private recovery record is secret material, not ordinary state."""
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.cli")
    import ingotron_agent.cli as cli_mod

    _write_enrolled_state(paths_mod)
    paths_mod.TOKEN_FILE_PREV.write_text("ignr1." + "P" * 52, encoding="ascii")
    paths_mod.ENROLLMENT_PENDING.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.ENROLLMENT_PENDING.write_text("private recovery credential", encoding="utf-8")
    shredded = []

    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli_mod, "_systemctl", lambda *_args: 0)

    def _record_shred(path):
        shredded.append(path)
        path.unlink()

    monkeypatch.setattr(cli_mod, "_shred", _record_shred)
    assert cli_mod.cmd_uninstall(SimpleNamespace(purge=True)) == cli_mod.EXIT_OK
    assert set(shredded) == {
        paths_mod.TOKEN_FILE,
        paths_mod.TOKEN_FILE_PREV,
        paths_mod.ENROLLMENT_PENDING,
    }


# --- `--ingest` and the removed `upgrade` ----------------------------------


_REENROLL_CODE = "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT"


def _reenroll_with(tmp_path, monkeypatch, extra):
    """Run `reenroll` on an enrolled sandbox and return the exchange target."""
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll", "ingotron_agent.cli")
    import ingotron_agent.cli as cli_mod
    import ingotron_agent.enroll as enroll_mod

    _write_enrolled_state(paths_mod)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    source_config = _write_vault_config(tmp_path, paths_mod, recordings)
    paths_mod.VAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.VAULT_CONFIG.write_bytes(source_config.read_bytes())
    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0)
    seen = {}

    def _exchange(**kwargs):
        seen.update(host=kwargs["host"], port=kwargs["port"])
        return {
            "robot_id": "rover-7",
            "credential": "ignr1." + "R" * 52,
            "credential_fingerprint": "replacement-credential",
            "ingest_endpoint": "ingest.vault.test:50052",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {},
        }

    monkeypatch.setattr(enroll_mod, "exchange_code", _exchange)
    rc = cli_mod.main(
        ["reenroll", "--vault", "https://query.vault.test", "--code", _REENROLL_CODE, *extra]
    )
    return rc, seen, cli_mod


def test_reenroll_exchanges_at_the_ingest_endpoint_not_the_vault_host(tmp_path, monkeypatch):
    """Query and ingest have separate public names; the code goes to --ingest."""
    rc, seen, cli_mod = _reenroll_with(
        tmp_path, monkeypatch, ["--ingest", "ingest.vault.test:50052"]
    )
    assert rc == cli_mod.EXIT_OK
    assert seen == {"host": "ingest.vault.test", "port": 50052}


def test_reenroll_without_ingest_uses_the_vault_host_and_port_50051(tmp_path, monkeypatch):
    rc, seen, cli_mod = _reenroll_with(tmp_path, monkeypatch, [])
    assert rc == cli_mod.EXIT_OK
    assert seen == {"host": "query.vault.test", "port": 50051}


@pytest.mark.parametrize(
    "bad",
    ["ingest.vault.test", "ingest.vault.test:0", "ingest.vault.test:65536",
     ":50051", "ingest vault:50051", "ingest.vault.test:5x"],
)
def test_a_malformed_ingest_is_refused_before_the_exchange(tmp_path, monkeypatch, capsys, bad):
    paths_mod = _sandbox(tmp_path, monkeypatch, "ingotron_agent.enroll")
    import ingotron_agent.enroll as enroll_mod

    def _boom(**_kwargs):
        raise AssertionError("exchange_code must not be reached")

    monkeypatch.setattr(enroll_mod, "exchange_code", _boom)
    with pytest.raises(enroll_mod.EnrollmentError, match="--ingest"):
        enroll_mod.enroll(
            vault_url="https://vault.test",
            code=_REENROLL_CODE,
            ca_path=tmp_path / "ca.crt",
            config_path=tmp_path / "vault.json",
            token_file=paths_mod.TOKEN_FILE,
            recordings_root=str(tmp_path / "recordings"),
            ingest=bad,
        )


def test_the_agent_has_no_upgrade_command(capsys):
    """One upgrade path: the vault's robot.sh --upgrade, anchored like install."""
    import ingotron_agent.cli as cli_mod

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["upgrade"])
    assert exc.value.code == 2
    assert "invalid choice: 'upgrade'" in capsys.readouterr().err
