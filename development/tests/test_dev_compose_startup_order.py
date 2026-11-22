from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_gc_waits_for_schema_and_bucket_initialization() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    dependencies = compose["services"]["gc"]["depends_on"]

    assert dependencies["postgres"]["condition"] == "service_healthy"
    assert dependencies["objectstore"]["condition"] == "service_healthy"
    assert dependencies["db-migrate"]["condition"] == "service_completed_successfully"
    assert (
        dependencies["objectstore-access-ready"]["condition"]
        == "service_completed_successfully"
    )


def test_gc_metrics_port_override_is_host_only() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    gc = compose["services"]["gc"]

    assert "METRICS_PORT=9108" in gc["environment"]
    assert (
        "${GC_BIND_HOST:-127.0.0.1}:${GC_METRICS_PORT:-9108}:9108"
        in gc["ports"]
    )
