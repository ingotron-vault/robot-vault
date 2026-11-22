"""The two terminal states, proven hermetically.

An enrolled robot in steady state, and a revoked robot's next upload. Both are
asserted here WITHOUT the Rust ingest server, against a real gRPC server over
real TLS, so they hold on any machine and in CI rather than only where the full
stack can be stood up. The live end-to-end gate
(packaging/robot-agent/tests/live_enrollment_gate.sh) proves the same two
states against the real vault; this file is what makes them a unit gate.

What is genuinely covered here that nothing else covers:

  * the whole credential chain, from a 0600 file named by `auth.token_file`
    through `resolve_auth_token` to the `authorization: Bearer ignr1.…` header
    the server actually receives -- over a verified TLS channel, with the CA
    supplied the way robot.sh supplies it;
  * that a revoked credential is refused by the server and that the refusal is
    visible to the client as UNAUTHENTICATED;
  * that `ingotron-agent-run` reports an upload failure as a NON-ZERO EXIT.
    The runner propagates the upload CLI's nonzero failure status.
"""

import json
import os
import subprocess
import sys
import time
from concurrent import futures
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_PACKAGING_ROOT = _THIS_DIR.parent
_REPO_ROOT = _PACKAGING_ROOT.parent.parent
_AGENT_LIB = _PACKAGING_ROOT / "agent" / "lib"

for _path in (
    _AGENT_LIB,
    _REPO_ROOT / "phases" / "common-config" / "src",
    _REPO_ROOT / "phases" / "01-robot-storage" / "src",
    _REPO_ROOT / "phases" / "02-robot-upload-agent" / "src",
):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

grpc = pytest.importorskip("grpc", reason="grpcio is required for the terminal-state gates")
pb = pytest.importorskip("upload_pb2", reason="run `make proto` first")
pb_grpc = pytest.importorskip("upload_pb2_grpc", reason="run `make proto` first")

#: A fake credential with the `ignr1.` prefix.
#: Distinctive so it can be searched for.
ROBOT_TOKEN = "ignr1." + "TERMINALSTATE7T3DZ8H1RN6VBX0S3EYA2GJT4KQ29WMF7T3D"


def _make_server_cert(tmp_path: Path):
    """A self-signed cert with an IP SAN for 127.0.0.1.

    The IP SAN is required, not optional: the client dials `127.0.0.1:<port>`
    and gRPC verifies the hostname. A CN-only certificate is rejected by
    modern gRPC, which is the same reason the vault's SAN strategy is
    explicit.
    """
    key = tmp_path / "server.key"
    crt = tmp_path / "server.crt"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-keyout", str(key), "-out", str(crt),
        ],
        check=True,
        capture_output=True,
    )
    return key.read_bytes(), crt.read_bytes(), crt


class RecordingUploadService(pb_grpc.UploadServiceServicer):
    """Records the metadata it was presented, and optionally refuses it.

    `ListUploads` -- not `HealthCheck` -- is the credential-carrying RPC used
    for the revoked case, and that choice is load-bearing rather than
    arbitrary. Measured against the landed server by the live gate
    (tests/live_enrollment_gate.sh, see its header): `health_check()` in
    main.rs never calls `resolve_request_identity`, and the gRPC interceptor
    only rejects a MALFORMED bearer -- a well-formed but never-issued,
    revoked, or decommissioned `ignr1.` token is deferred to the handler,
    which ignores it. A fabricated token gets `{"status":"healthy"}` every
    time. `ListUploads`, `GetUploadStatus` and `Upload`/`UploadSmall` do call
    it. So a test that "proved" authentication through HealthCheck would prove
    nothing, and mirroring that mistake here would enshrine it.
    """

    def __init__(self, *, revoked: bool = False):
        self.revoked = revoked
        self.seen_metadata = []

    def _check(self, context):
        self.seen_metadata.append(dict(context.invocation_metadata()))
        if self.revoked:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "credential revoked")

    def HealthCheck(self, request, context):  # noqa: N802
        # A carrier for "did the bearer reach the wire", nothing more. The real
        # server does not authenticate here; neither does this stand-in claim to.
        self._check(context)
        return pb.HealthCheckResponse(status="healthy", checks={"per_robot_concurrency": "2"})

    def ListUploads(self, request, context):  # noqa: N802
        self._check(context)
        return pb.ListUploadsResponse(uploads=[], next_page_token="")


def _serve(servicer, tmp_path):
    key_pem, crt_pem, crt_path = _make_server_cert(tmp_path)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    pb_grpc.add_UploadServiceServicer_to_server(servicer, server)
    credentials = grpc.ssl_server_credentials([(key_pem, crt_pem)])
    # Port 0: the kernel assigns. Never hardcode a port in the ephemeral range.
    port = server.add_secure_port("127.0.0.1:0", credentials)
    assert port != 0, "the kernel refused to assign a port"
    server.start()
    return server, port, crt_path


def _write_enrolled_tree(tmp_path: Path, port: int, ca_path: Path, token: str = ROBOT_TOKEN):
    """The exact on-disk shape robot.sh leaves behind (after its step 8)."""
    cred_dir = tmp_path / "etc" / "ingotron" / "credentials"
    cred_dir.mkdir(parents=True)
    token_file = cred_dir / "robot.token"
    # No trailing newline: the file IS the token.
    token_file.write_text(token, encoding="ascii")
    os.chmod(str(token_file), 0o600)

    recordings = tmp_path / "recordings"
    recordings.mkdir(exist_ok=True)

    config = tmp_path / "vault.json"
    config.write_text(
        json.dumps(
            {
                "robot_id": "rover-7",
                "paths": {"robot_root": str(recordings)},
                "server": {"host": "127.0.0.1", "port": port},
                "tls": {"enabled": True, "mtls": False, "ca_path": str(ca_path)},
                "auth": {"token_file": str(token_file)},
            }
        ),
        encoding="utf-8",
    )
    return config, token_file, recordings


# --- the credential chain, all the way to the wire -------------------------


def test_the_token_file_reaches_the_wire_as_a_bearer_over_verified_tls(tmp_path):
    """From a 0600 file to `authorization: Bearer ignr1.…` on a verified channel.

    Every link is real here: a real TLS handshake the client verifies against
    the CA path from vault.json, a real transport built the way the agent
    builds it, and a server that reports what it actually received. A unit test
    that stopped at `resolve_auth_token` would prove none of it.
    """
    from robot_upload_agent.core import UploadQueue

    servicer = RecordingUploadService()
    server, port, ca_path = _serve(servicer, tmp_path)
    try:
        config, token_file, recordings = _write_enrolled_tree(tmp_path, port, ca_path)

        queue = UploadQueue(root=recordings, vault_config_path=config)
        transport = queue._new_transport()
        try:
            result = transport.health_check()
        finally:
            transport.close()

        assert result.get("status") == "healthy", result
        assert servicer.seen_metadata, "the server saw no request at all"
        auth = servicer.seen_metadata[0].get("authorization")
        assert auth == f"Bearer {ROBOT_TOKEN}", f"server received {auth!r}"

        # The credential must not be anywhere except the token file.
        assert oct(os.stat(token_file).st_mode & 0o777) == "0o600"
        assert ROBOT_TOKEN not in config.read_text(encoding="utf-8")
    finally:
        server.stop(0).wait()


def test_a_trailing_newline_in_the_token_file_is_stripped_before_the_wire(tmp_path):
    """An operator who edits the token file with an editor gets a newline.

    Sending `Bearer ignr1.…\\n` would be rejected by the server for a reason
    that looks nothing like "your file has a newline", so the strip has to
    happen and has to be proven at the wire, not at the parser.
    """
    from robot_upload_agent.core import UploadQueue

    servicer = RecordingUploadService()
    server, port, ca_path = _serve(servicer, tmp_path)
    try:
        config, _token_file, recordings = _write_enrolled_tree(
            tmp_path, port, ca_path, token=ROBOT_TOKEN + "\n"
        )
        queue = UploadQueue(root=recordings, vault_config_path=config)
        transport = queue._new_transport()
        try:
            transport.health_check()
        finally:
            transport.close()
        assert servicer.seen_metadata[0].get("authorization") == f"Bearer {ROBOT_TOKEN}"
    finally:
        server.stop(0).wait()


# --- TERMINAL STATE: a revoked robot's next upload -------------------------


def test_a_revoked_credential_is_refused_and_the_refusal_is_visible(tmp_path):
    """The robot presents a credential the vault has revoked.

    Two things are asserted, and the second is the one that matters
    operationally: the server DID see the bearer (so this is a rejection, not a
    connectivity failure), and the client sees UNAUTHENTICATED rather than a
    silent empty result.
    """
    from robot_upload_agent.core import UploadQueue

    servicer = RecordingUploadService(revoked=True)
    server, port, ca_path = _serve(servicer, tmp_path)
    try:
        config, _token_file, recordings = _write_enrolled_tree(tmp_path, port, ca_path)
        queue = UploadQueue(root=recordings, vault_config_path=config)
        transport = queue._new_transport()
        try:
            # ListUploads, because that is an RPC the real server actually
            # authenticates -- see RecordingUploadService's docstring.
            with pytest.raises(grpc.RpcError) as exc:
                transport.stub.ListUploads(
                    pb.ListUploadsRequest(robot_id="rover-7"),
                    metadata=transport.auth_metadata,
                    timeout=5.0,
                )
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
            assert "revoked" in exc.value.details()

            # The credential WAS presented -- this is a rejection, not a
            # network failure, and an operator must be able to tell them apart.
            assert servicer.seen_metadata[0].get("authorization") == f"Bearer {ROBOT_TOKEN}"

            # `health_check` deliberately swallows RPC errors ("an agent that
            # cannot ask must still be able to upload"), so it reports {} here.
            # Pinning that keeps the swallow deliberate rather than accidental
            # -- and is a second reason it can never be an auth probe.
            assert transport.health_check() == {}
        finally:
            transport.close()
    finally:
        server.stop(0).wait()


# --- TERMINAL STATE: the runner's exit status ------------------------------


def _stub_clis(monkeypatch, upload_payload, rsm_status=0, rua_status=0):
    """Replace the vendored CLIs with stubs that print what a real run prints."""
    import robot_storage_manager.cli as rsm_cli
    import robot_upload_agent.cli as rua_cli

    def rsm_main(argv):
        print(json.dumps({"scanned": 1, "hashed": 1}))
        return rsm_status

    def rua_main(argv):
        print(json.dumps(upload_payload))
        return rua_status

    monkeypatch.setattr(rsm_cli, "main", rsm_main)
    monkeypatch.setattr(rua_cli, "main", rua_main)


def _runner_tree(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("INGOTRON_ROOT_PREFIX", str(tmp_path))
    import ingotron_agent.paths as paths_mod

    importlib.reload(paths_mod)
    import ingotron_agent.runner as runner_mod

    importlib.reload(runner_mod)

    config, token_file, recordings = _write_enrolled_tree(tmp_path, 50051, tmp_path / "ca.crt")
    (recordings / "one.mcap").write_bytes(b"\x00" * 32)
    return runner_mod, paths_mod, config


def test_enrolled_robot_in_steady_state_exits_zero_and_records_the_run(
    tmp_path, monkeypatch, capsys
):
    """TERMINAL STATE 1. A healthy run: exit 0, and a durable summary on disk.

    `state/last_run.json` exists because journald is volatile -- `status` has to
    be able to say what happened after a reboot.
    """
    runner_mod, paths_mod, config = _runner_tree(tmp_path, monkeypatch)
    _stub_clis(monkeypatch, {"uploaded": ["one.mcap"], "errors": [], "skipped": []})

    assert runner_mod.main(["--config", str(config)]) == runner_mod.EXIT_OK

    summary = json.loads(paths_mod.LAST_RUN.read_text(encoding="utf-8"))
    assert summary["exit_status"] == 0
    assert [s["stage"] for s in summary["stages"]] == ["scan", "hash", "upload"]
    assert all(s["ok"] for s in summary["stages"])
    assert summary["stages"][-1]["result"]["uploaded"] == 1

    # One JSON object per line, which is what makes the journal greppable.
    for line in capsys.readouterr().out.strip().splitlines():
        json.loads(line)


def test_a_revoked_robots_next_run_exits_non_zero(tmp_path, monkeypatch):
    """TERMINAL STATE 2.

    `rua upload` exits 1 when any file fails. The runner must propagate that
    status and record the refusal, so a robot whose credential was revoked
    does not report success on a five-minute timer, forever.
    """
    runner_mod, paths_mod, config = _runner_tree(tmp_path, monkeypatch)
    _stub_clis(
        monkeypatch,
        {
            "uploaded": [],
            "errors": ["one.mcap: UNAUTHENTICATED: credential revoked"],
            "skipped": [],
        },
        rua_status=1,
    )

    status = runner_mod.main(["--config", str(config)])
    assert status == runner_mod.EXIT_STAGE_FAILED, (
        "the run reported success while every upload was being refused"
    )

    summary = json.loads(paths_mod.LAST_RUN.read_text(encoding="utf-8"))
    assert summary["exit_status"] != 0
    upload_stage = summary["stages"][-1]
    assert upload_stage["stage"] == "upload"
    assert upload_stage["result"]["errors"], "the refusal was not recorded"
    assert "UNAUTHENTICATED" in upload_stage["result"]["errors"][0]


def test_a_failed_scan_stops_the_run_before_uploading(tmp_path, monkeypatch):
    """A failed scan makes the later stages meaningless, so they must not run.

    Uploading against a stale or partial scan is how a robot ships the wrong
    manifest set.
    """
    runner_mod, paths_mod, config = _runner_tree(tmp_path, monkeypatch)
    _stub_clis(monkeypatch, {"uploaded": [], "errors": []}, rsm_status=3)

    assert runner_mod.main(["--config", str(config)]) == runner_mod.EXIT_STAGE_FAILED
    summary = json.loads(paths_mod.LAST_RUN.read_text(encoding="utf-8"))
    assert [s["stage"] for s in summary["stages"]] == ["scan"]
    assert summary["stages"][0]["exit_status"] == 3


def test_a_second_concurrent_run_skips_rather_than_racing(tmp_path, monkeypatch, capsys):
    """The second invocation exits 0 with {"skipped":"locked"}.

    `UploadQueue` takes no lock of its own, so without this an operator running
    a manual upload during a timer run would race it. Exit 0 because the timer
    skipping while a run is in progress is the design working, not a fault.
    """
    import fcntl

    runner_mod, paths_mod, config = _runner_tree(tmp_path, monkeypatch)
    _stub_clis(monkeypatch, {"uploaded": [], "errors": []})

    paths_mod.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(str(paths_mod.LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        started = time.time()
        assert runner_mod.main(["--config", str(config)]) == runner_mod.EXIT_OK
        # It must not have blocked waiting for the lock.
        assert time.time() - started < 5
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert any(line.get("skipped") == "locked" for line in lines), lines
    # And it uploaded nothing: no run summary was written by the skipped run.
    assert not paths_mod.LAST_RUN.exists()
