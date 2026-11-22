#!/usr/bin/env bash
# Scan, hash and upload one robot directory under the robot data root, or every
# robot directory when the robot id is "all". Runs inside the dev `robot`
# container (see `make robot-upload`).
#
# The agent reads its identity only from the config's `robot_id`, so each
# robot directory is uploaded with a temporary copy of the base config that
# names that robot.
#
# Usage: development/upload-robot-dir.sh ROBOT_ID|all [BASE_CONFIG]
set -euo pipefail

robot_id=${1:-demo}
base_config=${2:-configs/vault.local.json}
data_root=${ROBOT_DATA_ROOT:-/tmp/robot_data}

if [ "$robot_id" = all ]; then
    mapfile -t robots < <(find "$data_root" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
    if [ "${#robots[@]}" -eq 0 ]; then
        echo "no robot directories under $data_root" >&2
        exit 1
    fi
else
    robots=("$robot_id")
fi

config_dir=$(mktemp -d)
trap 'rm -rf "$config_dir"' EXIT

for rid in "${robots[@]}"; do
    root="$data_root/$rid"
    config="$config_dir/$rid.json"
    BASE_CONFIG="$base_config" CONFIG="$config" ROBOT_ID="$rid" python3 - <<'PY'
import json, os
with open(os.environ["BASE_CONFIG"], encoding="utf-8") as handle:
    config = json.load(handle)
config["robot_id"] = os.environ["ROBOT_ID"]
with open(os.environ["CONFIG"], "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
PY
    echo "== $rid ($root)"
    rsm scan --root "$root" --config "$config"
    rsm hash --root "$root" --config "$config"
    # rua uploads one batch per call; repeat until nothing is left.
    while :; do
        rc=0
        out=$(rua upload --root "$root" --config "$config") || rc=$?
        echo "$out"
        if [ "$rc" -ne 0 ]; then
            echo "rua upload failed for $rid (exit $rc)" >&2
            exit 1
        fi
        if grep -q '"uploaded": null' <<<"$out"; then
            break
        fi
    done
done
