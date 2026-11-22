#!/usr/bin/env python3
"""The core image lock: release/core-images.lock.json.

The lock names the five core release images by digest, so a deployment (and a
Pro build layered on them) runs exactly the images that were tested:

    {
      "schema": 1,
      "version": "0.11.0",                  # VERSION of the source that built them
      "source_commit": "<40 hex>",          # commit the images were built from
      "images": {                           # local tag name -> immutable reference
        "robot-vault-toolkit":        "<repo>@sha256:<64 hex>",
        "robot-vault-ingest":         "<repo>@sha256:<64 hex>",
        "robot-vault-core-processor": "<repo>@sha256:<64 hex>",
        "robot-vault-core-query":     "<repo>@sha256:<64 hex>",
        "robot-vault-objectstore":    "<repo>@sha256:<64 hex>"
      }
    }

Commands:
  pull  --lock F --version V [--tag T]  pull each digest (skip if present), check
                                        the digest, tag it locally as <name>:T (T=V)
  check --lock F --version V --revision SHA NAME...
                                        release-up gate: each NAME:V exists and was
                                        built from SHA (OCI revision label) or is
                                        the lock's digest
  write --out F --version V --source-commit SHA --tag T
                                        write a lock from the local NAME:T images
  show  --lock F NAME                   print the lock's reference for NAME

A lock whose images are still null has not been released yet: pull refuses it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

NAMES = (
    "robot-vault-toolkit",
    "robot-vault-ingest",
    "robot-vault-core-processor",
    "robot-vault-core-query",
    "robot-vault-objectstore",
)
REF = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def fail(message: str) -> "None":
    print(f"core-images: {message}", file=sys.stderr)
    raise SystemExit(1)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(("docker", *args), text=True, capture_output=True, check=check)


def repo_digests(image: str) -> list[str]:
    result = docker("image", "inspect", "--format", "{{json .RepoDigests}}", image, check=False)
    if result.returncode != 0:
        return []
    return json.loads(result.stdout or "[]") or []


def load(path: Path, version: str | None, *, filled: bool) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read lock {path}: {exc}")
    if not isinstance(lock, dict) or lock.get("schema") != 1:
        fail(f"{path}: schema must be 1")
    images = lock.get("images")
    if not isinstance(images, dict) or set(images) != set(NAMES):
        fail(f"{path}: images must name exactly {', '.join(NAMES)}")
    if not filled:
        return lock
    if lock.get("version") is None or lock.get("source_commit") is None or None in images.values():
        fail(f"{path}: the lock is not filled by a release yet")
    if not isinstance(lock.get("version"), str) or (version and lock["version"] != version):
        fail(f"{path}: version {lock.get('version')!r} is not this release ({version})")
    if not isinstance(lock.get("source_commit"), str) or not SHA.fullmatch(lock["source_commit"]):
        fail(f"{path}: source_commit must be a 40-hex commit (lock not filled by a release yet?)")
    for name, ref in images.items():
        if not isinstance(ref, str) or not REF.fullmatch(ref):
            fail(f"{path}: {name} must be <repo>@sha256:<64 hex>, got {ref!r} (lock not filled by a release yet?)")
    return lock


def cmd_pull(args: argparse.Namespace) -> None:
    lock = load(args.lock, args.version, filled=True)
    for name, ref in lock["images"].items():
        if ref not in repo_digests(ref):
            # Pulling by digest is content-addressed: the daemon refuses any other bytes.
            pulled = docker("pull", "--quiet", ref, check=False)
            if pulled.returncode != 0:
                fail(f"pull {ref} failed: {pulled.stderr.strip()}")
        if ref not in repo_digests(ref):
            fail(f"{ref} is not present with that digest after the pull")
        tag = args.tag or args.version
        docker("tag", ref, f"{name}:{tag}")
        print(f"  {ref} -> {name}:{tag}")


def cmd_check(args: argparse.Namespace) -> None:
    lock = None
    if args.lock and args.lock.exists():
        candidate = load(args.lock, None, filled=False)
        if all(isinstance(v, str) and REF.fullmatch(v) for v in candidate["images"].values()):
            lock = candidate
    bad = []
    for name in args.names:
        image = f"{name}:{args.version}"
        result = docker("image", "inspect", "--format",
                        '{{index .Config.Labels "org.opencontainers.image.revision"}}', image, check=False)
        if result.returncode != 0:
            bad.append(f"{image} not found locally -- run 'make release-tag' or 'make core-images-pull' first")
            continue
        if result.stdout.strip() == args.revision:
            continue
        if lock and name in lock["images"] and lock["images"][name] in repo_digests(image):
            continue
        bad.append(f"{image} revision {result.stdout.strip()!r} != checkout {args.revision} and it is not "
                   "the digest in the core image lock -- rebuild and retag, or pull the lock")
    if bad:
        fail("\n  ".join(["refusing to deploy:"] + bad))


def cmd_write(args: argparse.Namespace) -> None:
    images = {}
    for name in NAMES:
        digests = [d for d in repo_digests(f"{name}:{args.tag}") if d.startswith(f"{name}@")] \
            or repo_digests(f"{name}:{args.tag}")
        if not digests:
            fail(f"{name}:{args.tag} has no digest (missing, or the daemon has no containerd image store)")
        images[name] = digests[0]
    lock = {"schema": 1, "version": args.version, "source_commit": args.source_commit, "images": images}
    args.out.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    load(args.out, args.version, filled=True)
    print(f"wrote {args.out}")


def cmd_show(args: argparse.Namespace) -> None:
    lock = load(args.lock, None, filled=True)
    if args.name == "source_commit":
        print(lock["source_commit"])
    elif args.name in lock["images"]:
        print(lock["images"][args.name])
    else:
        fail(f"unknown image {args.name}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("--version", required=True, help="the release version the lock must name")
    p.add_argument("--tag", help="local tag to give the images (default: --version)")
    p.set_defaults(func=cmd_pull)
    p = sub.add_parser("check")
    p.add_argument("--lock", type=Path)
    p.add_argument("--version", required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("write")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--source-commit", required=True)
    p.add_argument("--tag", required=True)
    p.set_defaults(func=cmd_write)
    p = sub.add_parser("show")
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("name", help="an image name, or source_commit")
    p.set_defaults(func=cmd_show)
    args = parser.parse_args(argv)
    for value in (getattr(args, "version", None), getattr(args, "tag", None)):
        if value is not None and not TAG.fullmatch(value):
            fail(f"{value!r} is not a valid image tag")
    if getattr(args, "source_commit", None) and not SHA.fullmatch(args.source_commit):
        fail("--source-commit must be a 40-hex commit")
    args.func(args)


if __name__ == "__main__":
    main()
