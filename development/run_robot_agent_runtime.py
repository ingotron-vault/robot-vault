"""Run the complete robot-agent runtime suite with mandatory prerequisites."""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "packaging" / "robot-agent" / "tests"


class FailOnSkip:
    """Turn every collected pytest skip into a failed runtime gate."""

    skipped: list[str]

    def __init__(self) -> None:
        self.skipped = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.skipped:
            session.config.pluginmanager.get_plugin("terminalreporter").write_sep(
                "!", "mandatory runtime suite cannot skip tests"
            )
            for nodeid in self.skipped:
                session.config.pluginmanager.get_plugin("terminalreporter").write_line(
                    f"SKIPPED: {nodeid}"
                )
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def main() -> int:
    missing_tools = [
        name for name in ("bash", "dash", "busybox", "openssl")
        if shutil.which(name) is None
    ]
    if missing_tools:
        print(
            "missing mandatory robot-agent test tools: " + ", ".join(missing_tools),
            file=sys.stderr,
        )
        return 2

    # These imports back existing importorskip() calls. Preflighting them makes
    # an absent dependency a clear failure before collection rather than green.
    for name in ("grpc", "upload_pb2", "upload_pb2_grpc", "enrollment_pb2"):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            print(f"missing mandatory robot-agent test import {name}: {exc}", file=sys.stderr)
            return 2

    return int(pytest.main(["-q", str(SUITE)], plugins=[FailOnSkip()]))


if __name__ == "__main__":
    raise SystemExit(main())
