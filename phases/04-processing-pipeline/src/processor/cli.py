import argparse
import json
import time
from prometheus_client import start_http_server
from pathlib import Path
from .core import ProcessorConfig, run_once
from .cdr_decode import check_cdr_decoder_available
from . import pro_stages
from robot_vault_config.edition import identity


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="processor", description="MCAP → Parquet processor")
    p.add_argument("--parquet-root", default="/app/uploads/processed/parquet")
    # Process all topics by default; users can still pass a comma-separated allowlist
    p.add_argument("--topics", default="")
    p.add_argument("--watch", action="store_true", help="Run repeatedly, scanning for new uploads")
    p.add_argument("--interval", type=int, default=10, help="Seconds between scans in watch mode")
    args = p.parse_args(argv)
    # Log a missing pure-Python ros2msg/CDR decoder once at startup. Individual
    # CDR messages remain counted failures and fail their upload ratio gate.
    check_cdr_decoder_available()
    # Fail fast, before the process accepts a single upload: edition core
    # refuses Pro stage flags, edition pro refuses a missing or mismatched Pro
    # package and an unknown PII_BACKEND.
    stages = pro_stages.load()
    if stages is not None:
        stages.validate_pii_backend()
    # One JSON identity line; the release readiness check compares it across services.
    print(json.dumps({"rv_identity": {"service": "processor", **identity()}}, sort_keys=True), flush=True)
    cfg = ProcessorConfig(
        parquet_root=Path(args.parquet_root),
        topics=[t.strip() for t in str(args.topics).split(",") if t.strip()],
    )
    # start metrics server
    try:
        start_http_server(8000)
    except Exception:
        pass
    if not args.watch:
        n = run_once(cfg)
        print({"processed_candidates": n})
        return 0
    # watch loop
    while True:
        try:
            n = run_once(cfg)
            print({"processed_candidates": n})
        except Exception as e:
            print({"error": str(e)})
        time.sleep(max(1, int(args.interval)))
    # unreachable
    # return 0


if __name__ == "__main__":
    raise SystemExit(main())
