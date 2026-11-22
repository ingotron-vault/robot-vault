"""Deterministic connection-quality boundaries for the benchmark validity gate."""

import pytest

from robot_upload_agent.smart_batching import (
    ConnectionMeasurements,
    ConnectionQuality,
    SmartBatcher,
    parse_ping_measurements,
)


@pytest.mark.parametrize(
    ("throughput_mbps", "expected"),
    [
        (0.999, ConnectionQuality.POOR),
        (1.0, ConnectionQuality.NORMAL),
        (9.999, ConnectionQuality.NORMAL),
        (10.0, ConnectionQuality.GOOD),
        (49.999, ConnectionQuality.GOOD),
        (50.0, ConnectionQuality.EXCELLENT),
    ],
)
def test_throughput_boundaries(throughput_mbps, expected):
    measured = ConnectionMeasurements(throughput_mbps=throughput_mbps)
    assert SmartBatcher.classify_connection(measured) is expected


@pytest.mark.parametrize(
    ("rtt_ms", "expected"),
    [
        (49.999, ConnectionQuality.EXCELLENT),
        (50.0, ConnectionQuality.GOOD),
        (149.999, ConnectionQuality.GOOD),
        (150.0, ConnectionQuality.NORMAL),
        (299.999, ConnectionQuality.NORMAL),
        (300.0, ConnectionQuality.POOR),
    ],
)
def test_rtt_boundaries(rtt_ms, expected):
    measured = ConnectionMeasurements(rtt_ms=rtt_ms)
    assert SmartBatcher.classify_connection(measured) is expected


@pytest.mark.parametrize(
    ("loss_percent", "expected"),
    [
        (0.0, ConnectionQuality.EXCELLENT),
        (0.001, ConnectionQuality.GOOD),
        (0.999, ConnectionQuality.GOOD),
        (1.0, ConnectionQuality.NORMAL),
        (2.999, ConnectionQuality.NORMAL),
        (3.0, ConnectionQuality.POOR),
    ],
)
def test_loss_boundaries(loss_percent, expected):
    measured = ConnectionMeasurements(loss_percent=loss_percent)
    assert SmartBatcher.classify_connection(measured) is expected


def test_worst_available_signal_wins():
    measured = ConnectionMeasurements(
        rtt_ms=20.0, throughput_mbps=75.0, loss_percent=3.0
    )
    assert SmartBatcher.classify_connection(measured) is ConnectionQuality.POOR


def test_no_measurement_retains_the_historical_normal_fallback():
    assert (
        SmartBatcher.classify_connection(ConnectionMeasurements())
        is ConnectionQuality.NORMAL
    )


def test_provider_is_cached_and_recent_upload_throughput_feeds_next_refresh():
    now = [100.0]
    calls = []

    def provider():
        calls.append(True)
        return ConnectionMeasurements(
            rtt_ms=20.0, loss_percent=0.0, source="test:ingest"
        )

    batcher = SmartBatcher(
        measurement_provider=provider, clock=lambda: now[0]
    )
    assert batcher.detect_connection_quality() is ConnectionQuality.EXCELLENT
    assert batcher.detect_connection_quality() is ConnectionQuality.EXCELLENT
    assert len(calls) == 1

    # 655,360 bytes/s is exactly 5 Mbit/s under the agent's MiB convention.
    batcher.record_upload_observation(
        bytes_uploaded=655_360, duration_seconds=1.0
    )
    now[0] += 31
    assert batcher.detect_connection_quality() is ConnectionQuality.NORMAL
    assert len(calls) == 2
    assert batcher.connection_measurements() == {
        "source": "test:ingest",
        "rtt_ms": 20.0,
        "throughput_mbps": 5.0,
        "loss_percent": 0.0,
    }


def test_iputils_ping_summary_yields_average_rtt_and_loss():
    output = """
5 packets transmitted, 5 received, 0% packet loss, time 4005ms
rtt min/avg/max/mdev = 199.101/200.250/201.722/0.812 ms
"""
    assert parse_ping_measurements(output, source="ping:ingest") == (
        ConnectionMeasurements(
            rtt_ms=200.25, loss_percent=0.0, source="ping:ingest"
        )
    )
