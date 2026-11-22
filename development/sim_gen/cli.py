"""`python -m sim_gen` -- the simulation MCAP generator CLI.

Prints the generated `.mcap` path (and nothing else) on stdout so shell callers
can capture it directly, e.g. `development/seed.sh`:

    MCAP=$(python3 -m sim_gen --out "$ROOT" --episodes 3 --duration 15 --seed 42)

The human-readable summary goes to stderr.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .generator import (
    COMPRESSION_CHOICES,
    DEFAULT_DURATION_SECONDS,
    DEFAULT_EPISODE_GAP_SECONDS,
    DEFAULT_JPEG_QUALITY,
    DEFAULT_ROBOT_ID,
    DEFAULT_SEED,
    DEFAULT_START_TIME_NS,
    SIM_GEN_VERSION,
    generate,
)
from .scene import DEFAULT_HEIGHT, DEFAULT_WIDTH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sim_gen",
        description=(
            "Generate a deterministic simulation MCAP of AMR episodes "
            "(CDR-encoded ROS 2 messages) plus a ground-truth sidecar JSON."
        ),
    )
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--episodes", type=int, default=3, help="number of episodes")
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help=f"seconds per episode (default {DEFAULT_DURATION_SECONDS:g})",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="RNG seed; output is a pure function of it"
    )
    parser.add_argument(
        "--bad-episode",
        type=int,
        default=None,
        metavar="K",
        help="1-based episode to degrade deliberately (see the sidecar for what was broken)",
    )
    parser.add_argument("--robot-id", default=DEFAULT_ROBOT_ID)
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=DEFAULT_EPISODE_GAP_SECONDS,
        help=(
            "message silence between episodes; must be > 10 so a 5 s gap-based "
            f"segmenter splits them (default {DEFAULT_EPISODE_GAP_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--start-time-ns",
        type=int,
        default=DEFAULT_START_TIME_NS,
        help="log_time of the first message (fixed by default, for determinism)",
    )
    parser.add_argument("--image-width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--image-height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--jpeg-quality", type=int, default=DEFAULT_JPEG_QUALITY)
    parser.add_argument(
        "--compression", choices=sorted(COMPRESSION_CHOICES), default="zstd"
    )
    parser.add_argument("--name", default=None, help="output file name (default derived from robot-id/seed)")
    parser.add_argument("--version", action="version", version=f"sim_gen {SIM_GEN_VERSION}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate(
            args.out,
            episodes=args.episodes,
            duration=args.duration,
            seed=args.seed,
            bad_episode=args.bad_episode,
            robot_id=args.robot_id,
            gap_seconds=args.gap_seconds,
            start_time_ns=args.start_time_ns,
            image_width=args.image_width,
            image_height=args.image_height,
            jpeg_quality=args.jpeg_quality,
            compression=args.compression,
            filename=args.name,
        )
    except ValueError as exc:
        print(f"sim_gen: {exc}", file=sys.stderr)
        return 2

    sidecar = result.sidecar
    print(
        f"sim_gen {SIM_GEN_VERSION}: {sidecar['episode_count']} episode(s), "
        f"{sidecar['message_count']} messages, {sidecar['mcap_bytes']} bytes, "
        f"sha256={sidecar['mcap_sha256'][:16]}...",
        file=sys.stderr,
    )
    for episode in sidecar["episodes"]:
        marker = " [DEGRADED]" if episode["degraded"] else ""
        print(
            f"  episode {episode['index']}: {episode['message_count']} msgs, "
            f"task={episode['task_label']!r}{marker}",
            file=sys.stderr,
        )
    print(f"  sidecar: {result.sidecar_path}", file=sys.stderr)
    print(result.mcap_path)
    return 0
