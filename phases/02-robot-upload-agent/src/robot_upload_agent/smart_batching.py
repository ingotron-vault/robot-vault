"""Measured connection policy and upload ordering.

Whole files share a bounded worker pool. Connection quality selects the
concurrency request and retry multiplier; selected files are ordered by
priority, then size. There is no separate batch queue.
"""
import threading
import time
import re
import subprocess
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum


class ConnectionQuality(Enum):
    """Network connection quality levels"""
    POOR = "poor"           # <1 Mbps, high latency
    NORMAL = "normal"       # 1-10 Mbps
    GOOD = "good"           # 10-50 Mbps
    EXCELLENT = "excellent" # >=50 Mbps, low latency


@dataclass
class BatchStrategy:
    """Per-quality concurrency request and retry multiplier."""
    parallel_streams: int
    retry_multiplier: float


@dataclass(frozen=True)
class ConnectionMeasurements:
    """Signals observed on the path to ingest.

    RTT and loss come from a short ping of the configured ingest host. The
    throughput value is the most recent successful whole-file upload observed
    by this process; it is intentionally not a configured link rate or a WiFi
    signal-strength guess.
    """

    rtt_ms: Optional[float] = None
    throughput_mbps: Optional[float] = None
    loss_percent: Optional[float] = None
    source: str = "unavailable"


_PING_LOSS_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)%\s+packet loss")
_PING_RTT_RE = re.compile(
    r"(?:rtt|round-trip)\s+[^=]*=\s*"
    r"[0-9.]+/([0-9.]+)/[0-9.]+(?:/[0-9.]+)?\s*ms"
)


def parse_ping_measurements(output: str, *, source: str) -> ConnectionMeasurements:
    """Parse iputils/BusyBox-style ping summary output without guessing."""

    loss_match = _PING_LOSS_RE.search(output or "")
    rtt_match = _PING_RTT_RE.search(output or "")
    return ConnectionMeasurements(
        rtt_ms=float(rtt_match.group(1)) if rtt_match else None,
        loss_percent=float(loss_match.group(1)) if loss_match else None,
        source=source,
    )


class SmartBatcher:
    """Connection measurements, file ordering, and per-quality upload policy."""

    # Strategies per detected connection quality.
    #
    # `parallel_streams` grows as the link gets worse. The opposite mapping -- 4
    # streams for EXCELLENT and 1 for POOR -- would be backwards for the
    # mechanism the agent actually implements. The unit of concurrency is a whole file on its OWN gRPC
    # channel, i.e. its own TCP connection, and independent connections help
    # exactly where a single congestion window cannot fill the pipe: high RTT
    # and/or loss. There, one connection's throughput is bounded by
    # ~MSS/(RTT*sqrt(p)) regardless of how much link capacity is idle, and N
    # connections recover close to N times that. On a fast, low-latency,
    # loss-free link a single stream already sits near line rate, so extra
    # connections buy nothing and only add memory and shared-fate failure
    # surface. Hence: most streams where the link is worst.
    #
    # (Adding streams to the SAME channel would buy nothing anywhere -- a gRPC
    # channel opens one TCP connection per subchannel, so N HTTP/2 streams share
    # one congestion-control state and one loss-recovery timeline. That is why
    # `core` builds an independent transport per worker.)
    #
    # The values are a *request*, not a grant: the effective count is further
    # bounded by `upload_agent.parallel_streams`, by the server's advertised
    # per-robot limit, and by MAX_PARALLEL_STREAMS.
    BATCH_STRATEGIES = {
        ConnectionQuality.POOR: BatchStrategy(
            parallel_streams=4,       # independent connections matter most here
            retry_multiplier=3.0      # back off hardest on the flakiest link
        ),
        ConnectionQuality.NORMAL: BatchStrategy(
            parallel_streams=3,
            retry_multiplier=2.0
        ),
        ConnectionQuality.GOOD: BatchStrategy(
            parallel_streams=2,
            retry_multiplier=1.5
        ),
        ConnectionQuality.EXCELLENT: BatchStrategy(
            parallel_streams=1,       # one stream already nears line rate
            retry_multiplier=1.0
        )
    }

    def __init__(
        self,
        *,
        probe_host: Optional[str] = None,
        measurement_provider: Optional[Callable[[], ConnectionMeasurements]] = None,
        clock: Callable[[], float] = time.time,
    ):
        # Connection quality detection
        self.probe_host = probe_host
        self._measurement_provider = measurement_provider or self.measure_ingest_path
        self._clock = clock
        self._quality_lock = threading.Lock()
        self._last_quality_check = 0
        self._cached_quality = ConnectionQuality.NORMAL
        self._last_measurements = ConnectionMeasurements()
        self._latest_throughput_mbps: Optional[float] = None

    @staticmethod
    def classify_connection(measurements: ConnectionMeasurements) -> ConnectionQuality:
        """Map measured signals to the existing four strategy buckets.

        Every available signal is classified independently and the worst one
        wins. Missing signals are ignored; when nothing is measurable the
        historical NORMAL fallback is retained and reported as unavailable.
        """

        ranks = {
            ConnectionQuality.EXCELLENT: 0,
            ConnectionQuality.GOOD: 1,
            ConnectionQuality.NORMAL: 2,
            ConnectionQuality.POOR: 3,
        }
        observed: List[ConnectionQuality] = []

        throughput = measurements.throughput_mbps
        if throughput is not None and throughput >= 0:
            observed.append(
                ConnectionQuality.POOR if throughput < 1
                else ConnectionQuality.NORMAL if throughput < 10
                else ConnectionQuality.GOOD if throughput < 50
                else ConnectionQuality.EXCELLENT
            )

        rtt = measurements.rtt_ms
        if rtt is not None and rtt >= 0:
            observed.append(
                ConnectionQuality.POOR if rtt >= 300
                else ConnectionQuality.NORMAL if rtt >= 150
                else ConnectionQuality.GOOD if rtt >= 50
                else ConnectionQuality.EXCELLENT
            )

        loss = measurements.loss_percent
        if loss is not None and loss >= 0:
            observed.append(
                ConnectionQuality.POOR if loss >= 3
                else ConnectionQuality.NORMAL if loss >= 1
                else ConnectionQuality.GOOD if loss > 0
                else ConnectionQuality.EXCELLENT
            )

        if not observed:
            return ConnectionQuality.NORMAL
        return max(observed, key=ranks.__getitem__)

    def measure_ingest_path(self) -> ConnectionMeasurements:
        """Measure RTT/loss to the configured ingest host with five probes."""

        if not self.probe_host:
            return ConnectionMeasurements()
        result = subprocess.run(
            ["ping", "-c", "5", "-W", "2", self.probe_host],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        return parse_ping_measurements(
            "\n".join((result.stdout or "", result.stderr or "")),
            source=f"ping:{self.probe_host}",
        )

    def detect_connection_quality(
        self, measurements: Optional[ConnectionMeasurements] = None
    ) -> ConnectionQuality:
        """Classify real path measurements, cached for 30 seconds."""

        if measurements is not None:
            return self.classify_connection(measurements)

        with self._quality_lock:
            now = self._clock()
            if now - self._last_quality_check < 30:
                return self._cached_quality
            try:
                measured = self._measurement_provider()
            except Exception:
                measured = ConnectionMeasurements()
            if measured.throughput_mbps is None:
                measured = ConnectionMeasurements(
                    rtt_ms=measured.rtt_ms,
                    throughput_mbps=self._latest_throughput_mbps,
                    loss_percent=measured.loss_percent,
                    source=measured.source,
                )
            self._last_measurements = measured
            self._cached_quality = self.classify_connection(measured)
            self._last_quality_check = now
            return self._cached_quality

    def record_upload_observation(
        self, *, bytes_uploaded: int, duration_seconds: float
    ) -> None:
        """Remember real successful-upload throughput for the next refresh."""

        if bytes_uploaded < 0 or duration_seconds <= 0:
            return
        throughput = (bytes_uploaded * 8) / (duration_seconds * 1024 * 1024)
        with self._quality_lock:
            self._latest_throughput_mbps = throughput

    def connection_measurements(self) -> Dict[str, Any]:
        """Serializable evidence behind the current quality label."""

        with self._quality_lock:
            measured = self._last_measurements
            return {
                "source": measured.source,
                "rtt_ms": measured.rtt_ms,
                "throughput_mbps": measured.throughput_mbps,
                "loss_percent": measured.loss_percent,
            }

    def order_files(self, files: List[Dict]) -> List[Dict]:
        """Prioritize critical files, then smallest on poor links, largest otherwise."""
        quality = self.detect_connection_quality()
        return sorted(files, key=lambda file: (
            0 if file.get("priority") == "critical" else 1,
            file.get("size", 0) if quality == ConnectionQuality.POOR else -file.get("size", 0),
        ))
