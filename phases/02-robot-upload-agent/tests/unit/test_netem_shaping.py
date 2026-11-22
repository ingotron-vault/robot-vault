"""The shaping harness must refuse to produce mislabelled numbers.

The measurement itself needs `NET_ADMIN` and a live stack and has NOT been
run — no shaped-link figure is published anywhere in this repo. What is
tested here is the part that decides whether a run may be trusted, because
that is the part whose failure mode is silent: a `tc` command that does not
take effect yields a full-speed loopback run which then gets written down as
"20 Mbit, 200 ms, 1% loss". A wrong number that looks like a measurement is
worse than no number, so `shaped()` fails closed on every path.

These tests need neither NET_ADMIN nor Docker: they drive the decision
functions and assert on the command that would be issued.
"""
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "bench"))

import netem  # noqa: E402


# --- the profiles documented by the phase benchmark harness --------------------------

def test_the_three_agreed_profiles_exist_with_the_agreed_values():
    assert set(netem.PROFILES) == {"good-wifi", "poor-wifi", "bad-lossy"}
    assert netem.PROFILES["good-wifi"] == netem.Profile("good-wifi", 100.0, 20.0, 0.0)
    assert netem.PROFILES["poor-wifi"] == netem.Profile("poor-wifi", 20.0, 200.0, 1.0)
    assert netem.PROFILES["bad-lossy"] == netem.Profile("bad-lossy", 5.0, 400.0, 3.0)


@pytest.mark.parametrize("name", ["good-wifi", "poor-wifi", "bad-lossy"])
def test_tc_command_carries_rate_delay_and_loss(name):
    profile = netem.PROFILES[name]
    cmd = " ".join(netem.tc_add_command("eth0", profile))
    assert "qdisc add dev eth0 root netem" in cmd
    assert f"rate {profile.rate_mbit}mbit" in cmd
    assert f"delay {profile.delay_ms}ms" in cmd
    if profile.loss_pct:
        assert f"loss {profile.loss_pct}%" in cmd
    else:
        assert "loss" not in cmd


# --- "is shaping actually in effect?" -------------------------------------

def test_unshaped_loopback_rtt_is_rejected_for_every_profile():
    """The exact failure this guard exists for: tc silently did nothing, so
    the link is still loopback (~0.05 ms) while the run claims a profile."""
    for profile in netem.PROFILES.values():
        assert not netem.rtt_matches(0.05, profile), (
            f"an unshaped 0.05 ms RTT was accepted as profile {profile.name}"
        )


@pytest.mark.parametrize("name,measured", [
    ("good-wifi", 20.4), ("poor-wifi", 198.0), ("bad-lossy", 410.0),
])
def test_a_correctly_shaped_link_is_accepted(name, measured):
    """The accept half: a guard that rejected everything would be 'safe' and
    would make the benchmark unrunnable."""
    assert netem.rtt_matches(measured, netem.PROFILES[name])


def test_a_profile_is_not_confused_with_a_neighbouring_one():
    """20 ms measured must not pass as the 200 ms profile, or a good-wifi run
    could be published as poor-wifi."""
    assert not netem.rtt_matches(20.0, netem.PROFILES["poor-wifi"])
    assert not netem.rtt_matches(200.0, netem.PROFILES["bad-lossy"])
    assert not netem.rtt_matches(400.0, netem.PROFILES["good-wifi"])


def test_qdisc_output_is_only_accepted_when_netem_is_attached():
    assert netem.qdisc_is_netem(
        "qdisc netem 8001: root refcnt 2 limit 1000 delay 200ms rate 20Mbit loss 1%"
    )
    # What an unshaped interface reports -- must NOT be taken for shaping.
    assert not netem.qdisc_is_netem("qdisc noqueue 0: root refcnt 2")
    assert not netem.qdisc_is_netem("qdisc pfifo_fast 0: root refcnt 2 bands 3")
    assert not netem.qdisc_is_netem("")


def test_ping_rtt_parsing():
    out = (
        "--- ingest ping statistics ---\n"
        "5 packets transmitted, 5 received, 0% packet loss, time 4005ms\n"
        "rtt min/avg/max/mdev = 199.123/200.456/201.789/0.512 ms\n"
    )
    assert netem.parse_ping_rtt_ms(out) == pytest.approx(200.456)
    assert netem.parse_ping_rtt_ms("no summary here") is None


# --- fails closed, and always tears down ----------------------------------

def test_shaped_refuses_when_the_measured_rtt_contradicts_the_profile(monkeypatch):
    """This is the whole point: the qdisc claims to be there, but the link
    measures unshaped, so the run must not proceed."""
    applied, cleared = [], []

    monkeypatch.setattr(netem.shutil, "which", lambda _: "/sbin/tc")
    monkeypatch.setattr(netem, "_run", lambda cmd, check=True: _fake_run(cmd, applied, cleared))
    monkeypatch.setattr(netem, "measure_rtt_ms", lambda host, count=5: 0.05)

    with pytest.raises(netem.ShapingError, match="does not appear to be in effect"):
        with netem.shaped("eth0", netem.PROFILES["poor-wifi"], verify_host="ingest"):
            pytest.fail("the body must never run when shaping is unverified")

    assert cleared, "the qdisc must be torn down even when verification fails"


def test_shaped_tears_down_when_the_benchmark_body_raises(monkeypatch):
    """Otherwise a failed run leaves the box shaped at 5 Mbit and every later
    measurement on that host is silently wrong."""
    applied, cleared = [], []
    monkeypatch.setattr(netem.shutil, "which", lambda _: "/sbin/tc")
    monkeypatch.setattr(netem, "_run", lambda cmd, check=True: _fake_run(cmd, applied, cleared))
    monkeypatch.setattr(netem, "measure_rtt_ms", lambda host, count=5: 200.0)

    with pytest.raises(ZeroDivisionError):
        with netem.shaped("eth0", netem.PROFILES["poor-wifi"], verify_host="ingest"):
            raise ZeroDivisionError("benchmark blew up")

    assert cleared


def test_shaped_yields_the_measured_rtt_so_results_can_record_it(monkeypatch):
    applied, cleared = [], []
    monkeypatch.setattr(netem.shutil, "which", lambda _: "/sbin/tc")
    monkeypatch.setattr(netem, "_run", lambda cmd, check=True: _fake_run(cmd, applied, cleared))
    monkeypatch.setattr(netem, "measure_rtt_ms", lambda host, count=5: 201.5)

    with netem.shaped("eth0", netem.PROFILES["poor-wifi"], verify_host="ingest") as info:
        assert info["measured_rtt_ms"] == 201.5
        assert info["profile"]["name"] == "poor-wifi"
    assert cleared


def test_missing_tc_is_a_hard_error_not_a_silent_unshaped_run(monkeypatch):
    monkeypatch.setattr(netem.shutil, "which", lambda _: None)
    with pytest.raises(netem.ShapingError, match="tc is not available"):
        with netem.shaped("eth0", netem.PROFILES["good-wifi"]):
            pytest.fail("must not run without tc")


def test_failure_to_apply_netem_points_at_the_missing_capability(monkeypatch):
    """`tc` without NET_ADMIN fails with a permission error; the message has
    to say what to do about it."""
    monkeypatch.setattr(netem.shutil, "which", lambda _: "/sbin/tc")

    def _run(cmd, check=True):
        if cmd[:3] == ["tc", "qdisc", "add"]:
            raise netem.ShapingError("RTNETLINK answers: Operation not permitted")
        return _FakeProc("")

    monkeypatch.setattr(netem, "_run", _run)
    with pytest.raises(netem.ShapingError, match="NET_ADMIN"):
        with netem.shaped("eth0", netem.PROFILES["good-wifi"]):
            pytest.fail("must not run when netem could not be applied")


# --- SIGTERM must not skip the teardown in `finally` -----------------------

def test_sigterm_handler_raises_systemexit_instead_of_dying_silently():
    """Python's default SIGTERM disposition kills the process immediately --
    no stack unwinding, no `finally`. During `--hold-seconds` that would
    leave the qdisc attached forever. The handler must convert SIGTERM into
    a normal exception so `shaped()`'s `finally` gets to run."""
    with pytest.raises(SystemExit) as exc_info:
        netem._raise_sigterm(signal.SIGTERM, None)
    assert exc_info.value.code == 128 + signal.SIGTERM


def test_main_installs_the_sigterm_handler_before_shaping(monkeypatch):
    """`main()` must register the handler before it ever calls `shaped()`,
    so a SIGTERM delivered at any point while the link is shaped -- not just
    during the hold sleep -- still tears it down. Never sends a real signal:
    `signal.signal` is monkeypatched to just record the registration, and the
    recorded handler is invoked directly."""
    registered = {}
    monkeypatch.setattr(
        netem.signal, "signal",
        lambda sig, handler: registered.setdefault(sig, handler),
    )
    # tc missing -> shaped() fails closed immediately, before touching the
    # network, so this never needs NET_ADMIN or a real interface.
    monkeypatch.setattr(netem.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        sys, "argv",
        ["netem.py", "--profile", "good-wifi", "--hold-seconds", "5"],
    )

    assert netem.main() == 2

    assert netem.signal.SIGTERM in registered, "SIGTERM handler was never registered"
    with pytest.raises(SystemExit):
        registered[netem.signal.SIGTERM](netem.signal.SIGTERM, None)


# --- helpers ---------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _fake_run(cmd, applied, cleared):
    if cmd[:3] == ["tc", "qdisc", "add"]:
        applied.append(cmd)
        return _FakeProc("")
    if cmd[:3] == ["tc", "qdisc", "del"]:
        cleared.append(cmd)
        return _FakeProc("")
    if cmd[:3] == ["tc", "qdisc", "show"]:
        return _FakeProc("qdisc netem 8001: root refcnt 2 delay 200ms rate 20Mbit")
    return _FakeProc("")
