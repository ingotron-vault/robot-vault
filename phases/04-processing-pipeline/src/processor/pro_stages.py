"""The one seam between the core processor and the Pro derived stages.

Core reads MCAP, writes Parquet and fences every write. Pro adds episode
segmentation, media encoding and QA as one "rider" on that same single pass.
Core calls the rider at fixed points in ``core._extract_minimal_topics_to_parquet``;
the Pro package holds the rider itself. There is no registry and
no hook list: one module, one rider.

Edition ``core`` never imports Pro. It refuses a Pro stage flag set to true,
before any claim, so a core deployment cannot silently skip work that its
configuration asks for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import ModuleType
from typing import Callable, Dict, Optional

from robot_vault_config.edition import load_pro, read_edition

#: Pro-only stage switches. Edition core refuses each one set to true.
PRO_STAGE_FLAGS = ("SEGMENTATION_ENABLED", "QA_ENABLED", "MEDIA_ENCODE_ENABLED")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class StageContext:
    """What the rider needs from core for one upload. Core owns every field."""

    upload_id: str
    robot_id: str
    attempt_id: str
    dsn: Optional[str]
    filesystem: object
    bucket: Optional[str]
    stats: Optional[Dict[str, object]]
    ensure_owned: Callable[[], None]
    set_stage_status: Callable[..., None]
    stage_error: type


def _refuse_pro_flags() -> None:
    for name in PRO_STAGE_FLAGS:
        raw = (os.environ.get(name) or "").strip()
        value = raw.lower()
        if not raw or value in _FALSE:
            continue
        if value in _TRUE:
            raise ValueError(
                f"{name}={raw} is incompatible with ROBOT_VAULT_EDITION=core; "
                "core has no episode, media or QA stage"
            )
        raise ValueError(
            f"{name} must be one of true/false, 1/0, yes/no, on/off; got {raw!r}"
        )


def load() -> Optional[ModuleType]:
    """The Pro processor module for edition pro; None for core.

    Raises for edition core with a Pro stage flag set, and (through
    ``load_pro``) for edition pro without a matching Pro package.
    """

    edition = read_edition()
    if edition == "core":
        _refuse_pro_flags()
        return None
    return load_pro("processor", edition)
