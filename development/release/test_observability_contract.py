#!/usr/bin/env python3
"""Static contracts that keep release alerts tied to shipped metrics."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RULES = (ROOT / "configs/prometheus/release-alerts.yml").read_text(encoding="utf-8")
ROUTING = (ROOT / "configs/prometheus/alertmanager.yml").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "deploy/docker-compose.release.yml").read_text(encoding="utf-8")

# Core alerts only. The Pro alerts (exporter) live in the Pro release overlay's
# own rules file and are checked by the Pro edition's test.
required_alerts = {
    "RobotVaultQuerySaturated",
    "RobotVaultQueryCapacityRejections",
    "RobotVaultQueryTimeouts",
    "RobotVaultVolumeNearFull",
    "RobotVaultVolumeCritical",
    "RobotVaultBacklogOld",
    "RobotVaultBackupMissing",
    "RobotVaultBackupStale",
    "RobotVaultBackupLastRunFailed",
    "RobotVaultCertificateExpiring",
    "RobotVaultOperationalCollectionFailed",
    "RobotVaultOperationalCollectionStale",
}
configured_alerts = set(re.findall(r"^\s*- alert: ([A-Za-z0-9_]+)$", RULES, re.MULTILINE))
missing_alerts = required_alerts - configured_alerts
assert not missing_alerts, f"missing release alerts: {sorted(missing_alerts)}"

metric_sources = {
    "query_service_query_active": ROOT / "phases/05-query-service/src/query_service/middleware.py",
    "query_service_query_capacity": ROOT / "phases/05-query-service/src/query_service/middleware.py",
    "query_service_query_admission_rejections_total": ROOT
    / "phases/05-query-service/src/query_service/middleware.py",
    "query_service_query_timeouts_total": ROOT
    / "phases/05-query-service/src/query_service/middleware.py",
    "robot_vault_volume_available_bytes": ROOT / "tools/operational_metrics.py",
    "robot_vault_volume_total_bytes": ROOT / "tools/operational_metrics.py",
    "robot_vault_backlog_age_seconds": ROOT / "tools/operational_metrics.py",
    "robot_vault_backlog_items": ROOT / "tools/operational_metrics.py",
    "robot_vault_backup_last_success_timestamp_seconds": ROOT / "tools/operational_metrics.py",
    "robot_vault_backup_last_run_success": ROOT / "tools/operational_metrics.py",
    "robot_vault_certificate_expiry_timestamp_seconds": ROOT / "tools/operational_metrics.py",
    "robot_vault_operational_collection_success": ROOT / "tools/operational_metrics.py",
    "robot_vault_operational_last_success_timestamp_seconds": ROOT / "tools/operational_metrics.py",
}
for metric, source_path in metric_sources.items():
    assert metric in RULES, f"release rules do not consume shipped metric {metric}"
    assert metric in source_path.read_text(encoding="utf-8"), (
        f"release rule references {metric}, but its producer is absent"
    )

assert "exporter" not in RULES, "core release rules name the Pro exporter"
assert "- /etc/prometheus/rules/*.yml" in COMPOSE and "- /etc/prometheus/scrape.d/*.yml" in COMPOSE
assert "- job_name: ops" in COMPOSE and "targets: ['ops-metrics:9110']" in COMPOSE
assert "--ops-only" in COMPOSE and "OPS_DATABASE_URL" in COMPOSE
assert "profiles: [\"obs\"]" in COMPOSE

for snippet in (
    "receiver: warning-operator",
    "receiver: critical-operator",
    'severity="critical"',
    'severity="warning"',
    "group_wait: 30s",
    "repeat_interval: 4h",
    "inhibit_rules:",
):
    assert snippet in ROUTING, f"Alertmanager routing contract omitted {snippet!r}"
assert "webhook_configs:" not in ROUTING and "email_configs:" not in ROUTING, (
    "repository template must not invent an operator delivery destination"
)
assert "ALERTMANAGER_IMAGE := prom/alertmanager:" in MAKEFILE
assert "--entrypoint /bin/amtool" in MAKEFILE
assert "check-config /alertmanager.yml" in MAKEFILE

print("release observability contract: ok")

# The reader and the backup tool agree on the file name; the compose mount feeds the reader's path.
METRICS = (ROOT / "tools/operational_metrics.py").read_text(encoding="utf-8")
BACKUP_TOOL = (ROOT / "tools/robot_vault_backup.py").read_text(encoding="utf-8")
assert "/run/robot-vault/backup-metrics/robot-vault-backup.prom" in METRICS
assert 'METRICS_FILE = "robot-vault-backup.prom"' in BACKUP_TOOL
assert "ingotron" not in METRICS and "appl" + "iance" not in RULES.lower()  # split: no literal in the source
assert "target: /run/robot-vault/backup-metrics" in COMPOSE
