"""The failure paths -- what happens when a step goes wrong halfway.

Happy-path tests say the design works. These say what it costs when it does
not, which for enrollment is the more important question: the enrollment code
is ONE-USE, and it is spent the instant the server commits. Anything that fails
after that point must leave the robot recoverable WITHOUT a new code, or a
transient disk error turns into an operator trip to the robot.
"""

import json
import os
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_PACKAGING_ROOT = _THIS_DIR.parent
_REPO_ROOT = _PACKAGING_ROOT.parent.parent
_AGENT_LIB = _PACKAGING_ROOT / "agent" / "lib"

for _path in (_AGENT_LIB, _REPO_ROOT / "phases" / "common-config" / "src"):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

#: The credential format is `ignr1.` + 52 Crockford characters = 58.
#: Asserted rather than eyeballed: `validate_credential` rejects any other length.
_SECRET_52 = "FAILUREPATHS" + "7T3DZ8H1RN6VBX0S3EYA2GJT4KQ29WMF7T3DZ8H1"
assert len(_SECRET_52) == 52, len(_SECRET_52)
ROBOT_TOKEN = "ignr1." + _SECRET_52
assert len(ROBOT_TOKEN) == 58, len(ROBOT_TOKEN)


def _reload(tmp_path, monkeypatch, *modules):
    import importlib

    monkeypatch.setenv("INGOTRON_ROOT_PREFIX", str(tmp_path))
    import ingotron_agent.paths as paths_mod

    importlib.reload(paths_mod)
    for name in modules:
        importlib.reload(importlib.import_module(name))
    return paths_mod


# --- secretstore -----------------------------------------------------------


@pytest.mark.parametrize("umask_value", [0o000, 0o022, 0o077, 0o777])
def test_the_token_is_0600_whatever_the_umask_was(tmp_path, umask_value):
    """open(..., 0600) is masked by the process umask; chmod is not.

    A robot installed from a shell with `umask 077` and one with `umask 000`
    must end up with the same 0600 file. Without the explicit chmod the second
    one gets 0600 anyway (mask only clears bits) -- but the DIRECTORY, created
    with mkdir + 0700, is the one that silently loses bits, and a 0777
    credentials directory is a real exposure.
    """
    from ingotron_agent import secretstore

    previous = os.umask(umask_value)
    try:
        target = tmp_path / "creds" / "robot.token"
        secretstore.write_secret(target, ROBOT_TOKEN)
        assert oct(target.stat().st_mode & 0o777) == "0o600"
        assert oct(target.parent.stat().st_mode & 0o777) == "0o700"
        assert target.read_bytes() == ROBOT_TOKEN.encode("ascii")
    finally:
        os.umask(previous)


def test_a_failed_replace_does_not_destroy_the_live_token(tmp_path, monkeypatch):
    """A rotation that dies mid-write must leave the OLD token usable.

    `robot.token` is the only thing standing between the robot and a trip to
    site, so the write goes to `.new` and renames over. If the rename never
    happens the original must still be there, intact and still 0600.
    """
    from ingotron_agent import secretstore

    target = tmp_path / "creds" / "robot.token"
    secretstore.write_secret(target, ROBOT_TOKEN)

    def _boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(secretstore.os, "replace", _boom)
    with pytest.raises(OSError):
        secretstore.write_secret(target, "ignr1." + "B" * 52, replace=True)

    assert target.read_text(encoding="ascii") == ROBOT_TOKEN
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_a_stale_new_file_does_not_block_the_next_rotation(tmp_path):
    """The `.new` left behind by a previous crash must not wedge the next try.

    O_EXCL on `<path>.new` would fail forever otherwise, and the robot would be
    stuck on an expiring credential with no way to replace it.
    """
    from ingotron_agent import secretstore

    target = tmp_path / "creds" / "robot.token"
    secretstore.write_secret(target, ROBOT_TOKEN)
    stale = Path(str(target) + ".new")
    stale.write_text("leftover from a crash", encoding="ascii")

    replacement = "ignr1." + "C" * 52
    secretstore.write_secret(target, replacement, replace=True)
    assert target.read_text(encoding="ascii") == replacement
    assert not stale.exists()


def test_a_first_write_refuses_to_clobber_an_existing_credential(tmp_path):
    """O_EXCL is what makes "enroll twice" a refusal rather than a silent swap."""
    from ingotron_agent import secretstore

    target = tmp_path / "creds" / "robot.token"
    secretstore.write_secret(target, ROBOT_TOKEN)
    with pytest.raises(FileExistsError):
        secretstore.write_secret(target, "ignr1." + "D" * 52)
    assert target.read_text(encoding="ascii") == ROBOT_TOKEN


# --- enroll: failure AFTER the code is spent -------------------------------


def test_a_vault_json_failure_after_enrolment_keeps_the_credential(tmp_path, monkeypatch):
    """The code is spent; the token is on disk; writing vault.json then fails.

    The credential MUST survive, the recorded facts MUST survive, and the error
    MUST tell the operator not to mint a new code -- because the instinct on
    seeing "enrollment failed" is to go get another one, and the old one is
    already burnt.
    """
    paths_mod = _reload(tmp_path, monkeypatch, "ingotron_agent.enroll")
    from ingotron_agent import enroll as enroll_mod

    def _fake_exchange(**_kwargs):
        return {
            "robot_id": "rover-7",
            "credential": ROBOT_TOKEN,
            "credential_fingerprint": "a3f01c9e2b44",
            "ingest_endpoint": "vault.local:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {
                "chunk_size_bytes": 8388608,
                "parallel_streams": 2,
                "min_stable_age_seconds": 30.0,
            },
        }

    def _boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(enroll_mod, "exchange_code", _fake_exchange)
    monkeypatch.setattr(enroll_mod, "write_vault_config", _boom)

    with pytest.raises(enroll_mod.EnrollmentError) as exc:
        enroll_mod.enroll(
            vault_url="https://vault.local:8003",
            code="ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
            ca_path=tmp_path / "ca.crt",
            config_path=tmp_path / "vault.json",
            token_file=paths_mod.TOKEN_FILE,
            recordings_root=str(tmp_path / "recordings"),
        )

    message = str(exc.value)
    assert "do not mint" in message.lower(), "the operator is not warned off a new code"
    assert str(paths_mod.TOKEN_FILE) in message

    # The credential survived, at the right mode.
    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == ROBOT_TOKEN
    assert oct(paths_mod.TOKEN_FILE.stat().st_mode & 0o777) == "0o600"

    # ...and so did the facts needed to finish without the vault.
    state = json.loads(paths_mod.ENROLLMENT_STATE.read_text(encoding="utf-8"))
    assert state["robot_id"] == "rover-7"
    assert state["ingest_endpoint"] == "vault.local:50051"
    assert state["vault_url"] == "https://vault.local:8003"
    assert state["self_rotation_enabled"] is False


def test_state_write_failure_after_first_enrolment_resumes_from_the_private_record(
    tmp_path, monkeypatch
):
    """A failed public state write cannot strand a one-use first enrollment.

    `enrollment.json` is deliberately readable by status and lives below a
    service-writable directory. The root-only pending record is written before
    the token, so retrying can finish without trusting a missing state file or
    repeating the exchange.
    """
    paths_mod = _reload(tmp_path, monkeypatch, "ingotron_agent.enroll")
    from ingotron_agent import enroll as enroll_mod

    exchanges = {"count": 0}

    def _fake_exchange(**_kwargs):
        exchanges["count"] += 1
        return {
            "robot_id": "rover-7",
            "credential": ROBOT_TOKEN,
            "credential_fingerprint": "a3f01c9e2b44",
            "ingest_endpoint": "vault.local:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {},
        }

    monkeypatch.setattr(enroll_mod, "exchange_code", _fake_exchange)
    original_write_state = enroll_mod._write_state
    monkeypatch.setattr(
        enroll_mod,
        "_write_state",
        lambda _state: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    recordings = tmp_path / "recordings"
    recordings.mkdir()

    with pytest.raises(enroll_mod.EnrollmentError, match="Do NOT mint"):
        enroll_mod.enroll(
            vault_url="https://vault.local:8003",
            code="ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
            ca_path=tmp_path / "ca.crt",
            config_path=paths_mod.VAULT_CONFIG,
            token_file=paths_mod.TOKEN_FILE,
            recordings_root=str(recordings),
        )

    assert paths_mod.ENROLLMENT_PENDING.is_file()
    assert not paths_mod.ENROLLMENT_STATE.exists()
    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == ROBOT_TOKEN

    monkeypatch.setattr(enroll_mod, "_write_state", original_write_state)
    state = enroll_mod.resume(
        config_path=paths_mod.VAULT_CONFIG,
        token_file=paths_mod.TOKEN_FILE,
        ca_path=tmp_path / "ca.crt",
        recordings_root=None,
    )
    assert state["robot_id"] == "rover-7"
    assert exchanges["count"] == 1
    assert not paths_mod.ENROLLMENT_PENDING.exists()


def test_recovery_record_failure_after_exchange_requires_a_fresh_code(
    tmp_path, monkeypatch
):
    """Without a durable credential copy, a consumed code cannot be retried safely."""
    paths_mod = _reload(tmp_path, monkeypatch, "ingotron_agent.enroll")
    from ingotron_agent import enroll as enroll_mod

    old_token = "ignr1." + "A" * 52
    paths_mod.CRED_DIR.mkdir(parents=True, exist_ok=True)
    paths_mod.TOKEN_FILE.write_text(old_token, encoding="ascii")
    os.chmod(paths_mod.TOKEN_FILE, 0o600)
    monkeypatch.setattr(
        enroll_mod,
        "exchange_code",
        lambda **_kwargs: {
            "robot_id": "rover-7",
            "credential": ROBOT_TOKEN,
            "credential_fingerprint": "a3f01c9e2b44",
            "ingest_endpoint": "vault.local:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": False,
            "recommended": {},
        },
    )
    monkeypatch.setattr(
        enroll_mod,
        "_write_recovery_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )

    with pytest.raises(enroll_mod.EnrollmentError, match="mint a fresh code"):
        enroll_mod.enroll(
            vault_url="https://vault.local:8003",
            code="ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
            ca_path=tmp_path / "ca.crt",
            config_path=tmp_path / "vault.json",
            token_file=paths_mod.TOKEN_FILE,
            recordings_root=str(tmp_path / "recordings"),
            force=True,
        )

    assert paths_mod.TOKEN_FILE.read_text(encoding="ascii") == old_token
    assert not paths_mod.ENROLLMENT_PENDING.exists()


def test_the_recorded_state_never_contains_the_credential(tmp_path, monkeypatch):
    """`enrollment.json` is 0644 and is read by `status`. It must be secret-free."""
    paths_mod = _reload(tmp_path, monkeypatch, "ingotron_agent.enroll")
    from ingotron_agent import enroll as enroll_mod

    monkeypatch.setattr(
        enroll_mod,
        "exchange_code",
        lambda **_k: {
            "robot_id": "rover-7",
            "credential": ROBOT_TOKEN,
            "credential_fingerprint": "a3f01c9e2b44",
            "ingest_endpoint": "vault.local:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": True,
            "recommended": {
                "chunk_size_bytes": 8388608,
                "parallel_streams": 2,
                "min_stable_age_seconds": 30.0,
            },
        },
    )
    (tmp_path / "recordings").mkdir()
    enroll_mod.enroll(
        vault_url="https://vault.local:8003",
        code="ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
        ca_path=tmp_path / "ca.crt",
        config_path=tmp_path / "vault.json",
        token_file=paths_mod.TOKEN_FILE,
        recordings_root=str(tmp_path / "recordings"),
    )

    for readable in (paths_mod.ENROLLMENT_STATE, tmp_path / "vault.json"):
        text = readable.read_text(encoding="utf-8")
        assert ROBOT_TOKEN not in text, f"{readable} contains the credential"
        assert "ignr1." not in text, f"{readable} contains something credential-shaped"
    assert oct(paths_mod.ENROLLMENT_STATE.stat().st_mode & 0o777) == "0o644"


def test_enroll_returns_no_credential_to_its_caller(tmp_path, monkeypatch):
    """`cmd_enroll` prints what enroll() returns, so it must not return the token."""
    paths_mod = _reload(tmp_path, monkeypatch, "ingotron_agent.enroll")
    from ingotron_agent import enroll as enroll_mod

    monkeypatch.setattr(
        enroll_mod,
        "exchange_code",
        lambda **_k: {
            "robot_id": "rover-7",
            "credential": ROBOT_TOKEN,
            "credential_fingerprint": "a3f01c9e2b44",
            "ingest_endpoint": "vault.local:50051",
            "supersedes_grace_seconds": 0,
            "self_rotation_enabled": True,
            "recommended": {
                "chunk_size_bytes": 8388608,
                "parallel_streams": 2,
                "min_stable_age_seconds": 30.0,
            },
        },
    )
    (tmp_path / "recordings").mkdir()
    state = enroll_mod.enroll(
        vault_url="https://vault.local:8003",
        code="ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT",
        ca_path=tmp_path / "ca.crt",
        config_path=tmp_path / "vault.json",
        token_file=paths_mod.TOKEN_FILE,
        recordings_root=str(tmp_path / "recordings"),
    )
    assert "credential" not in state
    assert ROBOT_TOKEN not in json.dumps(state)


# --- runner: a stage that explodes -----------------------------------------


def test_a_crashing_stage_is_recorded_rather_than_killing_the_run(
    tmp_path, monkeypatch, capsys
):
    """An exception out of a vendored CLI must become a failed stage, not a
    traceback out of a systemd timer with nothing written to last_run.json."""
    _reload(tmp_path, monkeypatch, "ingotron_agent.runner")
    from ingotron_agent import runner as runner_mod

    def _explode(_argv):
        raise RuntimeError("the scanner fell over")

    record = runner_mod._run_cli("scan", _explode, ["scan"])
    assert record["ok"] is False
    assert record["exit_status"] != 0
    assert "the scanner fell over" in record["stderr"]


def test_a_string_sys_exit_code_does_not_escape_the_wrapper(tmp_path, monkeypatch):
    """`SystemExit` may carry a string. int() on it would raise out of the
    handler and take down the whole run -- the one thing the wrapper exists to
    prevent."""
    _reload(tmp_path, monkeypatch, "ingotron_agent.runner")
    from ingotron_agent import runner as runner_mod

    def _exit_with_message(_argv):
        raise SystemExit("usage: rsm scan --root ...")

    record = runner_mod._run_cli("scan", _exit_with_message, ["scan"])
    assert record["ok"] is False
    assert isinstance(record["exit_status"], int)
