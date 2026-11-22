"""Unit tests for the robot-side agent runtime (packaging/robot-agent/agent/lib/ingotron_agent).

This package is not installed (it ships only inside the release tarball), so
its lib directory is added to sys.path below, computed from this file's own
location rather than assumed. ``robot_vault_config`` is imported from the
in-repo source tree the same way, since ``enroll.build_vault_config``'s output
is validated against the real loader.
"""

import fcntl
import json
import os
import stat
import sys
from pathlib import Path

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


# --- crockford.b32_prefix, against the shared conformance fixture ----------

def _load_b32_prefix_vectors():
    """Parse packaging/robot-agent/tests/fixtures/b32_prefix_vectors.tsv.

    Columns: hex, want, expected_rc, expected_out (tab-separated). Comment
    (#) and blank lines are skipped. The literal token ``<empty>`` in the hex
    column stands for the empty string (documented in the fixture's own
    header). expected_rc == 1 means the function must return None;
    expected_out is "-" in that case and carries no meaning.
    """
    fixture_path = _THIS_DIR / "fixtures" / "b32_prefix_vectors.tsv"
    vectors = []
    with fixture_path.open("r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split("\t")
            assert len(fields) == 4, (
                f"{fixture_path}:{lineno}: expected 4 tab-separated fields, got {fields!r}"
            )
            hex_field, want_field, rc_field, out_field = fields
            hex_digest = "" if hex_field == "<empty>" else hex_field
            vectors.append(
                pytest.param(
                    hex_digest,
                    int(want_field),
                    int(rc_field),
                    out_field,
                    id=f"line{lineno}:{hex_field}:{want_field}",
                )
            )
    return vectors


@pytest.mark.parametrize(
    "hex_digest, want, expected_rc, expected_out", _load_b32_prefix_vectors()
)
def test_b32_prefix_matches_shared_fixture_vector(hex_digest, want, expected_rc, expected_out):
    """Would break for any regression in crockford.b32_prefix -- the Rust
    minter, the POSIX-sh verifier and this Python encoder must all agree, or
    enrollment breaks end to end."""
    from ingotron_agent import crockford

    result = crockford.b32_prefix(hex_digest, want)
    if expected_rc == 1:
        assert result is None
    else:
        assert result == expected_out


# --- enroll.parse_code ------------------------------------------------------

_VALID_PIN = "0" * 26
_VALID_SECRET = "1" * 32
_VALID_CODE = f"ign1.{_VALID_PIN}.{_VALID_SECRET}"


def test_parse_code_accepts_well_formed_code():
    """Would break if parse_code stopped accepting a properly shaped ign1. code."""
    from ingotron_agent import enroll

    assert enroll.parse_code(_VALID_CODE) == {"pin": _VALID_PIN, "secret": _VALID_SECRET}


def test_parse_code_rejects_wrong_prefix():
    """Would break if parse_code stopped rejecting a code with any version tag
    other than ign1., e.g. a future ign2. format silently being accepted."""
    from ingotron_agent import enroll

    code = f"ign2.{_VALID_PIN}.{_VALID_SECRET}"
    assert len(code) == enroll.CODE_LENGTH
    with pytest.raises(enroll.EnrollmentError, match="ign1."):
        enroll.parse_code(code)


def test_parse_code_rejects_short_pin():
    """Would break if parse_code stopped validating the pin segment length
    (26 chars) before accepting a code."""
    from ingotron_agent import enroll

    pin = "0" * 25
    secret = "1" * 33  # keeps the overall code length at 64
    code = f"ign1.{pin}.{secret}"
    assert len(code) == enroll.CODE_LENGTH
    with pytest.raises(enroll.EnrollmentError):
        enroll.parse_code(code)


def test_parse_code_rejects_long_secret():
    """Would break if parse_code stopped validating the total code length /
    secret segment shape (32 chars)."""
    from ingotron_agent import enroll

    secret = "1" * 33
    code = f"ign1.{_VALID_PIN}.{secret}"
    with pytest.raises(enroll.EnrollmentError):
        enroll.parse_code(code)


@pytest.mark.parametrize("bad_char", ["I", "L", "O", "U"])
def test_parse_code_rejects_non_crockford_characters(bad_char):
    """Would break if parse_code stopped enforcing the Crockford base32
    alphabet -- I, L, O and U are all excluded and must all be rejected."""
    from ingotron_agent import enroll

    pin = bad_char + _VALID_PIN[1:]
    code = f"ign1.{pin}.{_VALID_SECRET}"
    assert len(code) == enroll.CODE_LENGTH
    with pytest.raises(enroll.EnrollmentError, match="Crockford"):
        enroll.parse_code(code)


def test_parse_code_rejects_total_length_mismatch():
    """Would break if parse_code stopped enforcing the fixed 64-character
    total code length."""
    from ingotron_agent import enroll

    code = _VALID_CODE[:-1]  # 63 characters
    assert len(code) != enroll.CODE_LENGTH
    with pytest.raises(enroll.EnrollmentError, match="64"):
        enroll.parse_code(code)


# --- enroll.build_vault_config ----------------------------------------------

def test_build_vault_config_produces_a_valid_enrolled_config(tmp_path):
    """Would break if build_vault_config's output stopped round-tripping
    through the real robot_vault_config loader, or stopped matching the
    enrolled-robot shape robot.sh step 8 installs: TLS on, mTLS off,
    no inline cert/key material, and the credential referenced by
    auth.token_file."""
    from ingotron_agent import enroll
    from robot_vault_config.model import load_config

    ca_path = tmp_path / "ca.crt"
    token_file = tmp_path / "credentials" / "robot.token"

    config = enroll.build_vault_config(
        robot_id="alpha-robot-01",
        ingest_endpoint="vault.example.com:50051",
        recordings_root="/robot_data",
        ca_path=ca_path,
        token_file=token_file,
        recommended={
            "chunk_size_bytes": 5 * 1024 * 1024,
            "parallel_streams": 2,
            "min_stable_age_seconds": 5.0,
        },
    )

    config_path = tmp_path / "vault.json"
    config_path.write_text(json.dumps(config))

    cfg = load_config(config_path)
    cfg.validate_tls()  # must not raise

    assert cfg.auth.token_file == str(token_file)
    assert cfg.tls.mtls is False
    assert cfg.tls.cert_path is None
    assert cfg.tls.key_path is None
    assert cfg.tls.enabled is True


# --- secretstore.write_secret ------------------------------------------------

def test_write_secret_creates_file_0600_no_trailing_newline_dir_0700(tmp_path):
    """Would break if write_secret stopped writing the exact mode/content
    contract: 0600 file, 0700 containing dir, content is
    exactly the token with no trailing newline."""
    from ingotron_agent import secretstore

    token_path = tmp_path / "credentials" / "robot.token"
    secretstore.write_secret(token_path, "ignr1.ABCDEF")  # gitleaks:allow (test value)

    assert token_path.read_bytes() == b"ignr1.ABCDEF"
    assert stat.S_IMODE(token_path.stat().st_mode) == secretstore.TOKEN_MODE
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == secretstore.DIR_MODE


def test_write_secret_second_write_without_replace_raises(tmp_path):
    """Would break if a second enrollment silently clobbered a live credential
    instead of refusing via O_EXCL."""
    from ingotron_agent import secretstore

    token_path = tmp_path / "credentials" / "robot.token"
    secretstore.write_secret(token_path, "ignr1.FIRST")

    with pytest.raises(FileExistsError):
        secretstore.write_secret(token_path, "ignr1.SECOND")  # gitleaks:allow (test value)

    assert token_path.read_bytes() == b"ignr1.FIRST"


def test_write_secret_with_replace_true_succeeds_and_updates_content(tmp_path):
    """Would break if the rotation path (replace=True) stopped being able to
    overwrite an existing credential file."""
    from ingotron_agent import secretstore

    token_path = tmp_path / "credentials" / "robot.token"
    secretstore.write_secret(token_path, "ignr1.FIRST")
    secretstore.write_secret(token_path, "ignr1.SECOND", replace=True)  # gitleaks:allow (test value)

    assert token_path.read_bytes() == b"ignr1.SECOND"


def test_write_secret_refuses_credential_containing_newline(tmp_path):
    """Would break if write_secret stopped refusing a credential containing a
    newline, which would corrupt the "file IS the token" contract."""
    from ingotron_agent import secretstore

    token_path = tmp_path / "credentials" / "robot.token"
    with pytest.raises(secretstore.SecretWriteError):
        secretstore.write_secret(token_path, "ignr1.BAD\n")


def test_write_secret_refuses_empty_credential(tmp_path):
    """Would break if write_secret stopped refusing an empty credential,
    which could otherwise persist a blank (unusable) token file."""
    from ingotron_agent import secretstore

    token_path = tmp_path / "credentials" / "robot.token"
    with pytest.raises(secretstore.SecretWriteError):
        secretstore.write_secret(token_path, "")


# --- runner.single_instance --------------------------------------------------

def test_single_instance_raises_lock_held_when_already_locked(tmp_path):
    """Would break if single_instance stopped detecting a held flock and
    instead blocked or silently proceeded, letting a manual run race a timer
    run against the same UploadQueue (core.py's `_upload_one` takes no lock
    of its own)."""
    from ingotron_agent import runner

    lock_path = tmp_path / "agent.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(runner.LockHeld):
            with runner.single_instance(lock_path):
                pass  # pragma: no cover - must not be reached
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- runner.main -------------------------------------------------------------

def test_main_with_nonexistent_config_returns_exit_config(tmp_path, capsys):
    """Would break if main() stopped fast-failing on a missing vault.json with
    EXIT_CONFIG, or stopped emitting the JSON diagnostic line the systemd
    journal relies on."""
    from ingotron_agent import runner

    missing_config = tmp_path / "vault.json"

    exit_code = runner.main(["--config", str(missing_config)])

    assert exit_code == runner.EXIT_CONFIG
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    assert lines
    payload = json.loads(lines[-1])
    assert payload["ok"] is False


# --- runner.assert_enrolled_posture -----------------------------------------

def test_assert_enrolled_posture_rejects_tls_disabled(tmp_path):
    """Would break if assert_enrolled_posture stopped refusing to run an
    enrolled robot with TLS disabled -- the guard that keeps a robot off
    insecure_channel."""
    from ingotron_agent import runner
    from robot_vault_config.model import TlsSettings, VaultConfig

    cfg = VaultConfig(tls=TlsSettings(enabled=False))
    config_path = tmp_path / "vault.json"

    with pytest.raises(SystemExit, match=str(runner.EXIT_CONFIG)):
        runner.assert_enrolled_posture(cfg, config_path)


def test_assert_enrolled_posture_rejects_tls_enabled_without_credential(tmp_path):
    """Would break if assert_enrolled_posture stopped requiring a named
    credential (auth.token_file) even when TLS is enabled."""
    from ingotron_agent import runner
    from robot_vault_config.model import TlsSettings, VaultConfig

    cfg = VaultConfig()
    cfg.tls = TlsSettings(enabled=True, mtls=False, ca_path=str(tmp_path / "ca.crt"))
    config_path = tmp_path / "vault.json"

    with pytest.raises(SystemExit):
        runner.assert_enrolled_posture(cfg, config_path)


def test_assert_enrolled_posture_accepts_well_formed_enrolled_config(tmp_path):
    """Would break if assert_enrolled_posture started rejecting a properly
    enrolled robot config (TLS enabled, CA path set, auth.token_file set)."""
    from ingotron_agent import runner
    from robot_vault_config.model import AuthSettings, TlsSettings, VaultConfig

    cfg = VaultConfig()
    cfg.tls = TlsSettings(enabled=True, mtls=False, ca_path=str(tmp_path / "ca.crt"))
    cfg.auth = AuthSettings(token_file=str(tmp_path / "credentials" / "robot.token"))
    config_path = tmp_path / "vault.json"

    runner.assert_enrolled_posture(cfg, config_path)  # must not raise
