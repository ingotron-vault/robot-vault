#!/usr/bin/env bash
# Source dependency vulnerability gate (`make dep-audit`).
#
# It scans the tracked dependency lock files before a release candidate
# exists. The release gate scans the built images later; this gate finds a
# vulnerable pin while it is still a one-line lock-file change.
#
#   Python and Rust: Grype 0.116.1 in directory mode over a staged copy of every
#                    tracked requirements*.lock* file and Cargo.lock. It is the
#                    same binary (SHA-256 a8fff88f...) and the same reviewed
#                    grype.yaml policy as the release image scan.
#   npm:             `npm audit --package-lock-only` on every tracked
#                    package-lock.json, development dependencies included.
#                    (Grype's npm cataloguer skips development dependencies.)
#
# evaluate.py then applies one policy to both sources: a fixable High or
# Critical finding fails the gate unless development/dep-audit/ignore.toml has
# a current, reviewed exception for it. Evidence goes to
# tmp/dep-audit/run-<UTC time>/; tmp/dep-audit/latest points to the last run.
#
# Exit status: 0 pass, 1 policy failure, 2 scanner or evidence error.
# All tools run in digest-pinned containers. Nothing is installed on the host.
set -euo pipefail

: "${NODE_CHECK_IMAGE:?set by make dep-audit}"
: "${PYTHON_CHECK_IMAGE:?set by make dep-audit}"

# The image index digest below holds the same linux/amd64 /grype binary
# (SHA-256 a8fff88f37a08af6a536e162f37f9902ec94af03df9928ee6295dffe7044dc43)
# that the release image gate requires. Checked 2026-10-08. grype.yaml here has
# the same bytes as the release gate's policy (pinned by the SHA-256 below).
GRYPE_IMAGE=anchore/grype:v0.116.1@sha256:1e71065c0a4cff3e6bd3b8add525ffac4343eb4971694eb90a31cf6d4d3e85db
GRYPE_POLICY=development/dep-audit/grype.yaml
GRYPE_POLICY_SHA256=31ad776576377f759a1323b37df06f157bcaabbfe185071365c9384d657b8e75

repo=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo"

fail() { echo "dep-audit: $*" >&2; exit 2; }

printf '%s  %s\n' "$GRYPE_POLICY_SHA256" "$GRYPE_POLICY" | sha256sum --check --strict --quiet \
  || fail "$GRYPE_POLICY does not match its reviewed SHA-256"

root=tmp/dep-audit
cache=$root/cache
stamp=$(date -u +%Y%m%dT%H%M%SZ)
run=$root/run-$stamp
mkdir -p "$cache/grype"
mkdir "$run" || fail "$run already exists"
mkdir -p "$run/inputs/grype" "$run/inputs/npm" "$run/npm-audit"

# Stage the scanned inputs. Only tracked files count; a stray local lock file
# must not change the result.
grype_inputs=0
while IFS= read -r -d '' path; do
  mkdir -p "$run/inputs/grype/$(dirname "$path")"
  cp -- "$path" "$run/inputs/grype/$path"
  grype_inputs=$((grype_inputs + 1))
done < <(git ls-files -z | grep -zE '(^|/)(requirements[^/]*\.lock[^/]*|Cargo\.lock)$')

npm_dirs=()
while IFS= read -r -d '' path; do
  dir=$(dirname "$path")
  npm_dirs+=("$dir")
  mkdir -p "$run/inputs/npm/$dir"
  cp -- "$path" "$run/inputs/npm/$path"
done < <(git ls-files -z | grep -zE '(^|/)package-lock\.json$')

[ "$grype_inputs" -gt 0 ] || fail "no Python or Cargo lock files are tracked"
[ "${#npm_dirs[@]}" -gt 0 ] || fail "no npm lock files are tracked"
printf '%s\n' "${npm_dirs[@]}" > "$run/npm-dirs.txt"

echo "dep-audit: Grype 0.116.1 on $grype_inputs Python/Cargo lock files"
# Ambient GRYPE_/SYFT_/ANCHORE_ overrides do not reach the container: only
# HOME (the database cache) is set. A non-zero exit is judged by evaluate.py.
set +e
docker run --rm --user "$(id -u):$(id -g)" --tmpfs /tmp:mode=1777 \
  -e HOME=/cache \
  -v "$repo/$cache/grype":/cache \
  -v "$repo/$GRYPE_POLICY":/policy/grype.yaml:ro \
  -v "$repo/$run/inputs/grype":/inputs:ro \
  -v "$repo/$run":/out \
  "$GRYPE_IMAGE" --config /policy/grype.yaml dir:/inputs \
  -o json=/out/grype.json -o cyclonedx-json=/out/inventory.cdx.json \
  2> "$run/grype.stderr"
grype_exit=$?
set -e
echo "$grype_exit" > "$run/grype.exit"

echo "dep-audit: npm audit on ${#npm_dirs[@]} npm lock files"
docker run --rm --user "$(id -u):$(id -g)" --tmpfs /tmp:mode=1777 \
  -e HOME=/tmp -e npm_config_update_notifier=false -e npm_config_fund=false \
  -v "$repo":/repo:ro -v "$repo/$run":/out -w /repo "$NODE_CHECK_IMAGE" \
  sh -c 'npm --version > /out/npm-audit/npm-version.txt
    while IFS= read -r dir; do
      name=$(printf "%s" "$dir" | tr "/." "__")
      npm audit --prefix "$dir" --package-lock-only --json \
        > "/out/npm-audit/$name.json" 2> "/out/npm-audit/$name.stderr"
      echo $? > "/out/npm-audit/$name.exit"
    done < /out/npm-dirs.txt'

set +e
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$repo/development/dep-audit":/tool:ro \
  -v "$repo/$run":/run-dir \
  "$PYTHON_CHECK_IMAGE" python -I /tool/evaluate.py \
  --run-dir /run-dir --ignore /tool/ignore.toml \
  --grype-image "$GRYPE_IMAGE" --grype-policy-sha256 "$GRYPE_POLICY_SHA256" \
  --report-path "$run/report.md"
status=$?
set -e
ln -sfn "run-$stamp" "$root/latest"
exit "$status"
