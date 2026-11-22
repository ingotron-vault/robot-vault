# Robot Vault example data

Use this public example bundle to exercise a new installation before a customer
robot uploads data. Run `make examples` to generate recordings for ingest and
processing. Pro adds segmentation, privacy handling, quality checks (QA),
dataset cuts, and export.

Every `.mcap` is a real MCAP recording with ROS 2 `ros2msg` schemas and CDR
payloads. The adjacent JSON sidecar is machine-readable; the adjacent datacard
is the human note explaining the robot, source, transformation, license, and
what to inspect. `manifest.json` pins every generated bag by SHA-256.

## The four recordings

| Label | Robot/source | Purpose |
| --- | --- | --- |
| `wheeled-nominal` | real TUM Pioneer excerpt | Cleanest path; real RGB, laser, odometry, and a measured Twist derived from the real pose trajectory with non-zero `angular_z`. |
| `manipulator-pickplace` | real LeRobot `aloha_static_coffee` excerpt | Full bimanual joint state/action, round-trip provenance, and explicit metric-exclusion honesty. |
| `imperfect-run` | engineered TUM derivative | Missing calibration, timestamp jitter, dropped streams, frozen/blurred frames, and no task label; with Pro, intended to produce DEFECT and RISK QA findings. |
| `pii-bearing` | fully synthetic | Fictional generated face and fictional plate in synthetic camera frames; exercises PII scrubbing (Pro) and the closed raw-media gate without using a real identity. |

The two TUM bags are licensed under CC BY 4.0 and carry the required TUM
credit in their datacards. The ALOHA bag carries its MIT source credit. The
sources were checked on 2026-08-10 against the primary pages below. Use this
attribution text when you copy or show a recording:

| Example | Source | License | Primary source | Attribution |
| --- | --- | --- | --- | --- |
| `wheeled-nominal` | TUM RGB-D Benchmark, `fr2/pioneer_slam2` | CC BY 4.0 | [TUM dataset license](https://cvg.cit.tum.de/data/datasets/rgbd-dataset), [TUM sequence download](https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download) | "Technical University of Munich, Computer Vision Group, TUM RGB-D Benchmark, sequence fr2/pioneer_slam2, CC BY 4.0." |
| `manipulator-pickplace` | LeRobot `aloha_static_coffee`, revision `b144896feb1f37398a862927b22cd3abdf005a6b` | MIT | [pinned Hugging Face dataset card](https://huggingface.co/datasets/lerobot/aloha_static_coffee/blob/b144896feb1f37398a862927b22cd3abdf005a6b/README.md), [ALOHA project](https://tonyzhaozh.github.io/aloha/) | "LeRobot aloha_static_coffee dataset (ALOHA; Tony Zhao, Vikash Kumar, Sergey Levine, and Chelsea Finn), MIT License." |
| `imperfect-run` | Changed copy of the same TUM excerpt | CC BY 4.0 | Same TUM sources | The TUM attribution, with "derivative" added. |
| `pii-bearing` | Robot Vault `sim_gen`, a generated fictional portrait and a drawn fictional plate | Project-owned synthetic fixture | [`assets/ASSET_PROVENANCE.md`](assets/ASSET_PROVENANCE.md) | "Robot Vault example-data contributors; no external person or vehicle imagery." |

## Privacy decision: synthetic PII only

`pii-bearing` contains **no real person, driver, customer, bystander, vehicle,
or license plate data**. Its portrait was generated for this fixture from a
text prompt with no input identity; its plate is a deterministic drawing with
the fictional text `SYN 2026`; its background and robot telemetry come from
Robot Vault's deterministic acceptance simulator.

This is deliberate. A permissive copyright license on a dashcam dataset can
clear redistribution of pixels while leaving privacy, biometric, publicity,
GDPR, BIPA, and similar rights of photographed people unresolved. Shipping
identifiable road users in a commercial product's demo bundle is a materially
different reuse. We therefore do not use Udacity, comma2k19, or any other real
road imagery for this feature, even where its copyright license is MIT.

With Pro PII scrubbing and a ready detector, every camera frame passes through
the detector and the media chunk earns `pii_status=scrubbed`. Without a ready
detector, the same bag remains `pii_status=raw`, and exports default closed.
Both processing outcomes are correct and observable; the bag never claims the
detector is infallible.

## Regenerate

The TUM source bag is about 1.4 GB. Regenerate on a machine with enough free disk
space and point the cache at that directory:

```bash
EXAMPLES_SOURCE_DIR=/roomy/cache/example-data make examples
```

The generator downloads the canonical TUM `fr2/pioneer_slam2` ROS bag and the
pinned ALOHA Parquet shard when absent. For already-downloaded or air-gapped
inputs:

```bash
PYTHONPATH=development python3 -m example_data \
  --no-download \
  --tum-bag /data/rgbd_dataset_freiburg2_pioneer_slam2.bag \
  --aloha-parquet /data/aloha_static_coffee-file-000.parquet \
  --out examples
```

Generation reuses `development/sim_gen`'s acceptance-tested MCAP writer and
CDR builders. It does not change or special-case the ingest or processor
pipeline.
