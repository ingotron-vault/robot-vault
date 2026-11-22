"""The benchmark harness must never label a server-capped arm as requested."""

import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parents[2] / "bench"
sys.path.insert(0, str(BENCH_DIR))
import upload_bench  # noqa: E402


def _status(*, requested=4, server=4, effective=3):
    return {
        "configured_parallel_streams": requested,
        "server_per_robot_streams": server,
        "server_streams_source": "advertised",
        "effective_parallel_streams": effective,
        "connection_quality": "normal",
        "connection_measurements": {
            "source": "ping:ingest",
            "rtt_ms": 200.0,
            "throughput_mbps": None,
            "loss_percent": 1.0,
        },
        "batch_strategy": {"parallel_streams": 3},
    }


def test_requested_streams_above_server_cap_fail_fast():
    with pytest.raises(SystemExit, match="requested parallel_streams=4 exceeds"):
        upload_bench.validate_parallel_stream_preflight(
            _status(requested=4, server=2, effective=2)
        )


def test_preflight_records_every_stream_bound_and_quality_evidence():
    assert upload_bench.validate_parallel_stream_preflight(_status()) == {
        "requested_parallel_streams": 4,
        "server_per_robot_streams": 4,
        "server_streams_source": "advertised",
        "effective_parallel_streams": 3,
        "connection_quality": "normal",
        "connection_measurements": {
            "source": "ping:ingest",
            "rtt_ms": 200.0,
            "throughput_mbps": None,
            "loss_percent": 1.0,
        },
        "strategy_parallel_streams": 3,
    }


@pytest.mark.parametrize(
    "field",
    [
        "configured_parallel_streams",
        "server_per_robot_streams",
        "effective_parallel_streams",
    ],
)
def test_missing_or_zero_stream_evidence_fails_closed(field):
    status = _status()
    status[field] = 0
    with pytest.raises(SystemExit, match="could not resolve"):
        upload_bench.validate_parallel_stream_preflight(status)


def test_assumed_server_cap_is_not_benchmark_evidence():
    status = _status()
    status["server_streams_source"] = "assumed"
    with pytest.raises(SystemExit, match="requires ingest's advertised stream cap"):
        upload_bench.validate_parallel_stream_preflight(status)
