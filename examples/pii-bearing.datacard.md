# pii-bearing example datacard

- **Robot ID:** `example-wheeled-01`
- **Robot type:** fully synthetic AMR privacy fixture
- **MCAP:** `pii-bearing.mcap` (1,831,279 bytes)
- **SHA-256:** `787c87fe25238a423631cd3da2902ac4b389aa224fa8ecde4ec87aa9e9f22051`
- **Encoding:** ROS 2 CDR payloads with `ros2msg` schemas in MCAP

## What it demonstrates

- face/license-plate scrubbing (Pro) with pii_status=scrubbed when a detector runs
- honest `pii_status=raw` when no detector runs; exports default closed
- the same bag makes both deployment paths observable

## What to inspect

- processor media metadata: pii_backend, pii_status, and regions blurred
- export gate behavior with and without detector-earned scrubbed status
- synthetic portrait and fictional plate visible in every camera frame before scrubbing

## Source, license, and attribution

- **Dataset:** Robot Vault deterministic sim_gen acceptance fixture plus generated fictional portrait
- **Source Url:** development/sim_gen and examples/assets/ASSET_PROVENANCE.md
- **License:** project-owned synthetic fixture
- **Attribution:** Robot Vault example-data contributors; no external person or vehicle imagery
- **Source Sha256:** 80263e38b5320ad943aff356489d11f1785d01056193f59c90c7b4da54fab949

## Transformations

- generated one deterministic eight-second sim_gen episode
- composited one fictional generated face and a deterministic fictional SYN 2026 plate into each synthetic frame
- preserved all non-camera acceptance-fixture messages byte-for-byte

## Notes and limitations

- SYNTHETIC ONLY: no real face, license plate, customer, driver, or bystander data is present.
- A permissive copyright license for real road imagery would not clear privacy, biometric, publicity, GDPR, or BIPA rights; real dashcam imagery is deliberately excluded.
- pii_status proves which backend ran over every encoded frame; it is not a claim that any detector is infallible.
