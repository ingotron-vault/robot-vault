"""Link shaping for the upload benchmarks.

Every number in `../docs/technical.md` was measured on single-host loopback:
multi-Gbit, ~0.05 ms RTT, no TLS, page-cache reads. So the repo publishes
**no** claim about behaviour on a poor connection — while `smart_batching.py`'s
whole premise is connection-quality adaptation and its
`parallel_streams` values (POOR 4 -> EXCELLENT 1) come from physics reasoning
rather than measurement. This module is what lets that table be tested.

It applies `tc qdisc ... netem` inside the container it runs in, which is why
`deploy/docker-compose.bench.yml` grants `NET_ADMIN` to the `robot` service **only**,
and why that grant is a separate overlay file that `docker compose up` can
never pick up implicitly.

THE TRAP THIS MODULE EXISTS TO AVOID. A `tc` command that silently fails, or
a qdisc attached to the wrong interface, produces a run at full loopback
speed which is then written down as a shaped-link result — the worst possible
outcome, because it is a wrong number that looks like a measurement. So
`shaped()` refuses to yield until it has *measured* the link and found the
profile actually in effect, and it always tears the qdisc down, including on
failure.

Netem emulates a link. It is not a robot radio: it does not reproduce
retransmit behaviour, contention, roaming, or the bursty loss patterns of a
real 2.4 GHz link in a warehouse. Numbers taken through it should be
published as netem numbers.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from typing import List, Optional


@dataclass(frozen=True)
class Profile:
    """One emulated link. Rates are what `tc` will be told to enforce."""

    name: str
    rate_mbit: float
    delay_ms: float
    loss_pct: float

    @property
    def expected_rtt_ms(self) -> float:
        """netem delay is applied on egress only, so a round trip through a
        single shaped interface sees it once, not twice."""
        return self.delay_ms


# The three profiles documented in this benchmark directory's README.
PROFILES = {
    "good-wifi": Profile("good-wifi", rate_mbit=100.0, delay_ms=20.0, loss_pct=0.0),
    "poor-wifi": Profile("poor-wifi", rate_mbit=20.0, delay_ms=200.0, loss_pct=1.0),
    "bad-lossy": Profile("bad-lossy", rate_mbit=5.0, delay_ms=400.0, loss_pct=3.0),
}


class ShapingError(RuntimeError):
    """Raised when shaping could not be applied or could not be verified.

    Deliberately fatal: a benchmark that continues here produces numbers
    labelled with a link that was never in effect.
    """


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ShapingError(
            f"{' '.join(cmd)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def tc_add_command(interface: str, profile: Profile) -> List[str]:
    """The netem command for `profile`. Split out so it can be asserted on
    without needing NET_ADMIN."""
    cmd = [
        "tc", "qdisc", "add", "dev", interface, "root", "netem",
        "delay", f"{profile.delay_ms}ms",
        "rate", f"{profile.rate_mbit}mbit",
    ]
    if profile.loss_pct > 0:
        cmd += ["loss", f"{profile.loss_pct}%"]
    return cmd


def qdisc_is_netem(show_output: str) -> bool:
    """True if `tc qdisc show dev X` reports a netem qdisc at the root."""
    return bool(re.search(r"qdisc\s+netem\s", show_output))


def parse_ping_rtt_ms(ping_output: str) -> Optional[float]:
    """Average RTT from `ping` summary output, or None if absent."""
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", ping_output)
    return float(m.group(1)) if m else None


def rtt_matches(measured_ms: float, profile: Profile, tolerance: float = 0.25) -> bool:
    """Is `measured_ms` consistent with the profile's configured delay?

    The check must do two things, and the second sets the bound. It has to
    catch "shaping is not in effect at all" — an unshaped loopback RTT is
    ~0.05 ms, hundreds of times below any profile — and it has to
    distinguish the profiles FROM EACH OTHER, so a run shaped as poor-wifi
    (200 ms) can never be recorded as bad-lossy (400 ms). At +/-50% the
    adjacent profiles overlap exactly at their boundary and 200 ms passes as
    400 ms; 25% separates every pair in PROFILES while still leaving far more
    slack than netem's delay accuracy needs on a loaded box.
    """
    expected = profile.expected_rtt_ms
    return abs(measured_ms - expected) <= expected * tolerance


def measure_rtt_ms(host: str, count: int = 5) -> float:
    if shutil.which("ping") is None:
        raise ShapingError(
            "ping is not installed in this container, so shaping cannot be "
            "verified -- refusing to run rather than publish unverified numbers"
        )
    proc = _run(["ping", "-c", str(count), "-q", host], check=False)
    rtt = parse_ping_rtt_ms(proc.stdout)
    if rtt is None:
        raise ShapingError(
            f"could not parse an RTT from ping {host}: {proc.stdout.strip()}"
        )
    return rtt


def clear(interface: str) -> None:
    """Remove any root qdisc. Never raises: used on the teardown path."""
    _run(["tc", "qdisc", "del", "dev", interface, "root"], check=False)


@contextmanager
def shaped(interface: str, profile: Profile, verify_host: Optional[str] = None,
           verify: bool = True):
    """Apply `profile` to `interface` for the duration of the block.

    Verifies shaping is really in effect before yielding, and always tears it
    down afterwards -- including when the body raises, which is the case that
    would otherwise leave a box shaped at 5 Mbit after a failed run.
    """
    if shutil.which("tc") is None:
        raise ShapingError("tc is not available (install iproute2)")

    clear(interface)  # a leftover qdisc from a crashed run would stack
    try:
        _run(tc_add_command(interface, profile))
    except ShapingError as exc:
        raise ShapingError(
            f"could not apply netem to {interface}: {exc}. NET_ADMIN is "
            f"granted only by deploy/docker-compose.bench.yml -- is the overlay in use?"
        ) from exc

    try:
        show = _run(["tc", "qdisc", "show", "dev", interface]).stdout
        if not qdisc_is_netem(show):
            raise ShapingError(
                f"netem is not attached to {interface} after adding it "
                f"(tc reports: {show.strip()})"
            )

        if verify and verify_host:
            rtt = measure_rtt_ms(verify_host)
            if not rtt_matches(rtt, profile):
                raise ShapingError(
                    f"shaping does not appear to be in effect: measured "
                    f"{rtt:.1f} ms RTT to {verify_host}, expected about "
                    f"{profile.expected_rtt_ms:.1f} ms for profile "
                    f"'{profile.name}'. Refusing to run -- an unshaped run "
                    f"labelled as '{profile.name}' is worse than no result."
                )
            yield {"profile": asdict(profile), "measured_rtt_ms": round(rtt, 2)}
        else:
            yield {"profile": asdict(profile), "measured_rtt_ms": None}
    finally:
        clear(interface)


def _raise_sigterm(signum, frame) -> None:
    """Turn SIGTERM into a normal Python exception.

    Python's default disposition for SIGTERM is to kill the process
    immediately -- no stack unwinding, no `finally` blocks. During
    `--hold-seconds` that would skip `shaped()`'s teardown entirely and leave
    the box shaped after the process is gone. Raising SystemExit here instead
    lets the `finally` in `shaped()` run like any other exit from the `with`
    block, so `docker compose down`, a CI timeout, or an operator's Ctrl-C
    forwarded as SIGTERM still clears the qdisc.
    """
    raise SystemExit(128 + signum)


def main() -> int:
    ap = argparse.ArgumentParser(description="apply/verify a netem profile")
    ap.add_argument("--profile", choices=sorted(PROFILES), required=True)
    ap.add_argument("--interface", default="eth0")
    ap.add_argument("--verify-host", default="ingest",
                    help="host to ping to confirm shaping is in effect")
    ap.add_argument("--hold-seconds", type=float, default=0.0,
                    help="keep the shaping applied this long (for manual runs)")
    args = ap.parse_args()

    # Registered before `shaped()` is ever entered, not just around the hold
    # sleep: a SIGTERM landing while `tc` is being applied or verified must
    # tear down too, not only one that lands during the hold.
    signal.signal(signal.SIGTERM, _raise_sigterm)

    profile = PROFILES[args.profile]
    try:
        with shaped(args.interface, profile, args.verify_host) as info:
            print(json.dumps(info))
            if args.hold_seconds:
                time.sleep(args.hold_seconds)
    except ShapingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
