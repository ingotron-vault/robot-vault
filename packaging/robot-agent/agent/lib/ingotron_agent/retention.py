"""`ingotron-agent-retention` -- the daily retention pass.

Runs `rsm retention plan` and LOGS THE PLAN WITHOUT DELETING unless
`retention.auto_apply` is explicitly true in vault.json.  Auto-deleting a
customer's recordings on day one is not a default, so the flag has to be
switched on deliberately and the plan-only path is what ships.
"""

import sys
from pathlib import Path
from typing import List, Optional

from . import paths
from .runner import EXIT_CONFIG, EXIT_OK, EXIT_STAGE_FAILED, _run_cli, emit, load_vault


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config_path = paths.VAULT_CONFIG
    if "--config" in args:
        config_path = Path(args[args.index("--config") + 1])
    if not config_path.is_file():
        emit({"stage": "retention", "ok": False, "error": f"{config_path} does not exist"})
        return EXIT_CONFIG

    cfg = load_vault(config_path)
    root = cfg.paths.robot_root
    auto_apply = bool(getattr(cfg.retention, "auto_apply", False))

    from robot_storage_manager.cli import main as rsm_main

    common = ["--config", str(config_path), "--root", str(root)]
    record = _run_cli("retention-plan", rsm_main, ["retention", "plan", *common])
    record["auto_apply"] = auto_apply
    emit(record)
    if not record["ok"]:
        return EXIT_STAGE_FAILED

    if not auto_apply:
        emit(
            {
                "stage": "retention",
                "ok": True,
                "applied": False,
                "reason": "retention.auto_apply is not true in vault.json; the plan above was "
                "logged and nothing was deleted",
            }
        )
        return EXIT_OK

    # `rsm retention apply` is a dry run WITHOUT --execute, so the flag has to
    # be passed explicitly here; auto_apply meaning "print a second plan" would
    # be the worst of both worlds.
    applied = _run_cli("retention-apply", rsm_main, ["retention", "apply", "--execute", *common])
    emit(applied)
    return EXIT_OK if applied["ok"] else EXIT_STAGE_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
