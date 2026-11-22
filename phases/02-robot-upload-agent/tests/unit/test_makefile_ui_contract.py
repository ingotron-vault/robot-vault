"""Fresh-checkout wiring for the generated robot SPA."""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[4]
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _prerequisites(target: str) -> set[str]:
	match = re.search(rf"^{re.escape(target)}:\s*([^\n]*)$", MAKEFILE, re.MULTILINE)
	assert match is not None, f"missing Make target: {target}"
	return set(match.group(1).split())


def test_stack_up_builds_gitignored_spa_before_mounting_the_robot_source() -> None:
	assert "ui-build" in _prerequisites("stack-up")


def test_robot_ui_start_builds_gitignored_spa_before_startup() -> None:
	assert "ui-build" in _prerequisites("robot-ui-start")
