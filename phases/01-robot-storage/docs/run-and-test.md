# Phase 01 — Run and test

Use this runbook to install `rsm`, create a sample recording, and check
retention behavior. Run commands from the repository root unless shown otherwise.

## Prerequisites

- Python 3.10+
- A virtual environment for development dependencies

Create and activate the environment:
```
cd <repo-root>
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Install the CLI (editable mode)

From the repository root:
```
cd <repo-root>
pip install -e phases/01-robot-storage
```

Check that the command is available:
```
cd <repo-root>
rsm --help
```

## Prepare sample data
```
cd <repo-root>
mkdir -p /tmp/robot_data/rover-7/2025-08-14
head -c 1048576 </dev/urandom > /tmp/robot_data/rover-7/2025-08-14/drive_001.mcap
```

## Scan and hash (with shared config)
```
cd <repo-root>
rsm scan --root /tmp/robot_data/rover-7 --config configs/vault.local.json
# The sample was written synchronously and its writer is closed, so this demo
# explicitly bypasses the normal five-second fresh-file guard.
rsm hash --root /tmp/robot_data/rover-7 --config configs/vault.local.json --missing-only --min-stable-age-seconds 0
rsm list --root /tmp/robot_data/rover-7
```

## Plan retention (dry-run, using config defaults)
```
cd <repo-root>
rsm retention plan --root /tmp/robot_data/rover-7 --config configs/vault.local.json
```

## Apply retention (dangerous; deletes files)
```
cd <repo-root>
rsm retention apply --root /tmp/robot_data/rover-7 --config configs/vault.local.json --execute
```

The command exits non-zero if a concurrent file/manifest change invalidates
the plan or protected data prevents reaching the requested capacity. Inspect
`failed`, `capacity_target_met_after_apply`, and the protected/at-risk fields.

## Run the phase checks
```
cd <repo-root>
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest phases/01-robot-storage/tests/unit -q
```

Disabling plugin autoload avoids import errors from unrelated third-party
pytest plugins, such as ROS plugins.

## Makefile shortcuts

For a host virtual environment, follow
[Optional host tooling](../../../development/README.md#optional-host-tooling).
`make test-unit` runs the unit suites in Docker:

```
cd <repo-root>
make test-unit
```

## Troubleshooting
- If `rsm` is not found, ensure your virtualenv is active and the package installed with `-e`.
- Use `--help` on any command to see available options.
