# Phase 02 — Run and test

Use this runbook to install the robot upload agent, run it against the local
development stack, and check an interrupted upload. Run commands from the
repository root unless a `cd` is shown.

## Prerequisites
- Python 3.10+
- Phase 01 manifests present under a root path

## Setup
```
cd /path/to/robot-vault
pip install -r requirements-dev.txt
pip install -e phases/02-robot-upload-agent
make ui-build  # required before serving the browser UI; generated assets are not committed
```

## Generate gRPC stubs (once per protocol change)

Generate the remote procedure call (gRPC) Python stubs after changing
`proto/upload.proto`:
```
cd /path/to/robot-vault
python -m grpc_tools.protoc -I proto \
  --python_out=phases/02-robot-upload-agent/src \
  --grpc_python_out=phases/02-robot-upload-agent/src \
  proto/upload.proto
```

## Sample config (shared)
JSON file (for example `configs/vault.local.json` for the local client/server
smoke path; Compose starts its datastore dependencies too):
```
{
  "robot_id": "rover-7",
  "paths": { "robot_root": "/robot_data", "extension": ".mcap" },
  "retention": { "min_retain_days": 14, "max_total_gib": 200 },
  "upload_agent": { "concurrency": 2, "small_file_threshold_mb": 4, "bandwidth_mbps": 0, "compute_chunk_hashes": true },
  "server": { "host": "ingest", "port": 50051 },
  "tls": { "enabled": false }
}
```

## Manual run
```
cd /path/to/robot-vault
rua status --root /tmp/robot_data/rover-7 --config configs/vault.local.json
rua upload --root /tmp/robot_data/rover-7 --config configs/vault.local.json
```

### Resumable upload (local dev)
```
# Reset a manifest to pending
docker exec -i robot-vault-robot bash -lc "python - << 'PY'
import json
p='/tmp/robot_data/rover-7/bag.mcap.manifest.json'
d=json.load(open(p))
d['status']='pending'; d['upload_id']=None
open(p,'w').write(json.dumps(d,indent=2,sort_keys=True))
print('ok')
PY"

# Start upload and interrupt by restarting ingest
docker exec -d robot-vault-robot bash -lc "rua upload --root /tmp/robot_data/rover-7 --config configs/vault.local.json > /tmp/rua.log 2>&1"
sleep 5 && docker restart robot-vault-ingest | cat
sleep 2

# Resume
docker exec -i robot-vault-robot bash -lc "rua upload --root /tmp/robot_data/rover-7 --config configs/vault.local.json | cat"
```
The retry sends only parts that are not already saved.
An expired multipart session requires the full file again.

## Tests
```
cd /path/to/robot-vault
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest phases/02-robot-upload-agent/tests/unit -q
```
