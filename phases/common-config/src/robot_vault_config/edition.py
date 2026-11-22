"""Canonical Robot Vault edition parsing shared by first-party services.

An edition is a deployment property, not a best-effort hint.  Unset means
core (the open-source edition); an unknown non-empty value is a startup/config
error because guessing either direction can expose Pro surface or remove paid
functionality.

The Pro code is a separate package (``robot_vault_pro``).  Core reaches it only
through ``load_pro``: edition ``core`` never imports it, and edition ``pro``
refuses to run without it or with a build made for another core.

``python -m robot_vault_config.edition`` prints ``identity()`` as JSON.
"""

from __future__ import annotations

import json
import os
from importlib import import_module
from types import ModuleType
from typing import Any, Mapping, Optional

DEFAULT_EDITION = "core"  # open-core default; Pro deployments set ROBOT_VAULT_EDITION=pro
EDITIONS = ("core", "pro")
CORE_VERSION = "0.11.0"  # test_edition keeps it equal to the VERSION file
ADAPTER_API = 1
PRO_PACKAGE = "robot_vault_pro"


def read_edition(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve ``ROBOT_VAULT_EDITION`` (default ``core``), or fail on a typo."""

    source = os.environ if env is None else env
    raw = (source.get("ROBOT_VAULT_EDITION") or "").strip().lower()
    edition = raw or DEFAULT_EDITION
    if edition not in EDITIONS:
        raise ValueError(
            f"ROBOT_VAULT_EDITION={source.get('ROBOT_VAULT_EDITION')!r} is not "
            f"a known edition; expected one of {', '.join(EDITIONS)}"
        )
    return edition


def _resolve(edition: Optional[str]) -> str:
    return read_edition() if edition is None else read_edition({"ROBOT_VAULT_EDITION": edition})


def _pro_package(edition: str) -> Optional[ModuleType]:
    """The checked Pro package for edition ``pro``; None (no import) for ``core``."""

    if edition != "pro":
        return None
    try:
        pro = import_module(PRO_PACKAGE)
    except ModuleNotFoundError as exc:
        # Only the package itself being absent means "not installed"; a missing
        # dependency inside Pro is a broken install and must surface as-is.
        if exc.name != PRO_PACKAGE:
            raise
        raise RuntimeError(
            "ROBOT_VAULT_EDITION=pro but the Pro package is not installed"
        ) from exc
    for pro_name, core_name, want in (
        ("ADAPTER_API", "ADAPTER_API", ADAPTER_API),
        ("BUILT_FOR_CORE", "CORE_VERSION", CORE_VERSION),
    ):
        have = getattr(pro, pro_name, None)
        if have != want:
            raise RuntimeError(
                f"{PRO_PACKAGE}.{pro_name}={have!r} does not match "
                f"core {core_name}={want!r}"
            )
    return pro


def load_pro(component: str, edition: Optional[str] = None) -> Optional[ModuleType]:
    """``robot_vault_pro.<component>`` for edition ``pro``; None for ``core``."""

    pro = _pro_package(_resolve(edition))
    return None if pro is None else import_module(f"{PRO_PACKAGE}.{component}")


def identity(edition: Optional[str] = None) -> dict[str, Any]:
    """What this process runs: edition, versions and Pro capabilities."""

    resolved = _resolve(edition)
    pro = _pro_package(resolved)
    return {
        "edition": resolved,
        "core_version": CORE_VERSION,
        "adapter_api": ADAPTER_API,
        "pro_version": None if pro is None else pro.PRO_VERSION,
        "capabilities": [] if pro is None else sorted(pro.CAPABILITIES),
    }


if __name__ == "__main__":
    print(json.dumps(identity(), sort_keys=True))
