"""CLI for the four public Robot Vault example recordings."""

from __future__ import annotations

import argparse
from pathlib import Path

from .generator import (
    ALOHA_FILENAME,
    TUM_FILENAME,
    generate_examples,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m example_data",
        description=(
            "Build the four public Robot Vault example MCAPs from a pinned TUM "
            "Pioneer bag, a pinned LeRobot ALOHA Parquet shard, and the existing "
            "synthetic acceptance generator."
        ),
    )
    parser.add_argument("--out", default="examples", help="bundle output directory")
    parser.add_argument(
        "--source-dir",
        default=".cache/example-data",
        help="large source cache (keep this off disk-constrained machines)",
    )
    parser.add_argument("--tum-bag", help=f"existing source bag (default: SOURCE_DIR/{TUM_FILENAME})")
    parser.add_argument(
        "--aloha-parquet",
        help=f"existing ALOHA shard (default: SOURCE_DIR/{ALOHA_FILENAME})",
    )
    parser.add_argument(
        "--synthetic-face",
        default="examples/assets/synthetic-person.png",
        help="fictional generated portrait used only by pii-bearing",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="refuse network downloads when a source is absent",
    )
    parser.add_argument(
        "--tum-start-seconds",
        type=float,
        default=None,
        help="source-relative window start; default auto-selects a real turn",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=8.0,
        help="TUM excerpt duration in seconds (default: 8)",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    source_dir = Path(args.source_dir)
    manifest = generate_examples(
        out_dir=Path(args.out),
        source_dir=source_dir,
        tum_bag=Path(args.tum_bag) if args.tum_bag else source_dir / TUM_FILENAME,
        aloha_parquet=(
            Path(args.aloha_parquet)
            if args.aloha_parquet
            else source_dir / ALOHA_FILENAME
        ),
        synthetic_face=Path(args.synthetic_face),
        download=not args.no_download,
        tum_start_seconds=args.tum_start_seconds,
        duration_seconds=args.duration,
    )
    print(
        f"example_data v1: {len(manifest['bags'])} bags, "
        f"{manifest['total_bytes']} bytes -> {Path(args.out).resolve()}"
    )
    for bag in manifest["bags"]:
        print(
            f"  {bag['label']}: {bag['bytes']} bytes, "
            f"sha256={bag['sha256'][:16]}..., messages={bag['message_count']}"
        )
    return 0
