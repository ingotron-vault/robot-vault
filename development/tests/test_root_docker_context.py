"""The developer image must never absorb the whole checkout.

Docker does not honor .gitignore. A developer can have valid local secrets and
tool state in ignored paths, so both the Dockerfile and build-context filter
must fail closed.
"""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_root_dockerfile_uses_an_explicit_copy_allowlist() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    broad_copies = {
        "COPY . /app",
        "COPY ./ /app",
        "ADD . /app",
        "ADD ./ /app",
    }
    lines = {line.strip() for line in dockerfile.splitlines()}
    assert lines.isdisjoint(broad_copies)

    for required in (
        "COPY proto /app/proto",
        "COPY db /app/db",
        "COPY configs /app/configs",
        "COPY tools /app/tools",
        "COPY development/sim_gen /app/development/sim_gen",
        "COPY development/example_data /app/development/example_data",
    ):
        assert required in lines
    assert "COPY development /app/development" not in lines


def test_root_context_recursively_excludes_local_state() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    required = {
        "**/*CREDENTIALS.local*",
        "**/node_modules/",
        "**/.agents/",
        "**/.codex/",
        "**/.cache/",
    }
    assert required <= patterns


def test_source_release_jobs_use_the_allowlisted_toolkit() -> None:
    compose = (ROOT / "deploy/docker-compose.release.yml").read_text(encoding="utf-8")

    assert "image: robot-vault-dev:" not in compose
    services = yaml.safe_load(compose)["services"]
    toolkit_services = {
        name for name, service in services.items()
        if str(service.get("image", "")).startswith("robot-vault-toolkit:")
    }
    assert toolkit_services == {
        "db-migrate",
        "postgres-roles-ready",
        "gc",
        "ops-roles-ready",
        "ops-metrics",
        "objectstore-access-ready",
    }


def test_core_images_are_built_from_core_paths_only() -> None:
    """Core images must build from a tree that has no pro/ directory."""
    for relative in (
        "Dockerfile",
        "phases/03-ingest-server-rs/Dockerfile",
        "phases/04-processing-pipeline/Dockerfile",
        "phases/05-query-service/Dockerfile",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        copies = [
            line.split()[1]
            for line in text.splitlines()
            if line.strip().upper().startswith(("COPY ", "ADD ")) and "--from=" not in line
        ]
        assert copies, relative
        for source in copies:
            assert source != "pro" and not source.startswith(("pro/", "private")), (relative, source)
        for pro_name in ("robot_vault_pro", "exporter", "ffmpeg"):
            assert pro_name not in text.lower(), (relative, pro_name)
