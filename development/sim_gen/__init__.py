"""Deterministic simulation MCAP generator for AMR episodes.

Dev tooling, not a shipped phase package: it lives under `development/` and its
tests deliberately do NOT join the five-directory unit gate (see
`development/sim_gen/README.md` and docs/TESTING.md for the honest labeling).

The dev image puts `/app/development` on `PYTHONPATH`, so `python -m sim_gen`
works anywhere inside it.
"""

from .generator import (  # noqa: F401  (re-exported)
    SIM_GEN_VERSION,
    TASK_LABELS,
    TOPICS,
    GenerateResult,
    degradation_windows,
    generate,
)

__all__ = [
    "SIM_GEN_VERSION",
    "TASK_LABELS",
    "TOPICS",
    "GenerateResult",
    "degradation_windows",
    "generate",
]
