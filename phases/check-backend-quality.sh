#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

mode="${1:-all}"
if [[ "$mode" == "all" || "$mode" == "python" ]]; then
    python3 -m mypy \
        --cache-dir "${TMPDIR:-/tmp}/robot-vault-mypy-${UID}" \
        --config-file mypy-backend.ini
fi
if [[ "$mode" == "all" || "$mode" == "rust" ]]; then
    (
        cd phases/03-ingest-server-rs
        cargo fmt --all -- --check
        cargo clippy --locked --all-targets --all-features -- -D warnings
    )
fi
if [[ "$mode" != "all" && "$mode" != "python" && "$mode" != "rust" ]]; then
    echo "usage: $0 [all|python|rust]" >&2
    exit 2
fi
