"""Frozen Linux agent entry point; kept free of host/runtime fallbacks."""

from ingotron_agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
