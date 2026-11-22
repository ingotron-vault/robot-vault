# `sim_gen` — deterministic simulation MCAP generator

Use `sim_gen` to make the standard camera-bearing test fixture when a physical
robot is unavailable. The generated MCAP recordings exercise the extractors
and, in Pro, the episode, QA and export stages.

It writes **one `.mcap` file** containing N AMR episodes separated by more than 10 s of
message silence — so the implemented gap-based segmenter
(`EPISODE_GAP_SECONDS=5`) splits them cleanly — plus a **ground-truth sidecar JSON**
next to it.

Every generated file contains `CompressedImage`, so Pro never exports it as
raw whole-source MCAP. This is a privacy boundary, not a fixture limitation.

## Quick start

```bash
# The standard fixture (3 episodes x 15 s, episode 2 deliberately bad):
docker compose run --rm --no-deps dev \
  python -m sim_gen --out /app/tmp_simgen --episodes 3 --duration 15 --seed 42 --bad-episode 2
```

If Docker group membership was added in the current login session and the
plain command still gets a daemon permission error, run that command through
`sg docker -c '...'` once or start a fresh login session. `sg` is a Linux
permission workaround, not part of the generator interface.

```
python -m sim_gen --out DIR --episodes N --duration SECS --seed S
                  [--bad-episode K] [--robot-id ID]
                  [--gap-seconds G] [--start-time-ns NS]
                  [--image-width W] [--image-height H] [--jpeg-quality Q]
                  [--compression zstd|lz4|none] [--name FILE]
```

The `.mcap` path goes to **stdout** (nothing else does), so shell callers can capture
it directly; the summary goes to stderr. `development/seed.sh` uses exactly that.

## What it emits

CDR-encoded ROS 2 messages with `ros2msg` schemas whose names match
`robot_vault_config.topics.ROS2_TYPE_NORMALIZATION` — anything else and the processor
silently skips every message on the topic.

| Topic | Type | Rate |
|---|---|---|
| `/camera/image_compressed` | `sensor_msgs/msg/CompressedImage` | 10 Hz (JPEG, 640x480 by default) |
| `/camera/camera_info` | `sensor_msgs/msg/CameraInfo` | 10 Hz |
| `/cmd_vel` | `geometry_msgs/msg/Twist` | 10 Hz (commanded) |
| `/odom` | `nav_msgs/msg/Odometry` | 20 Hz (achieved) |
| `/joint_states` | `sensor_msgs/msg/JointState` | 20 Hz (2 wheel joints) |
| `/tf` | `tf2_msgs/msg/TFMessage` | 20 Hz (`odom`→`base_link`) |
| `/tf_static` | `tf2_msgs/msg/TFMessage` | once per episode (`base_link`→`camera_link`) |
| `/imu/data` | `sensor_msgs/msg/Imu` | 50 Hz |
| `/gps/fix` | `sensor_msgs/msg/NavSatFix` | 5 Hz |
| `/session/task` | `std_msgs/msg/String` | once at each episode start — the task label |

`/cmd_vel`, `/odom`, `/joint_states`, `/tf` and `/imu/data` all come from **one**
differential-drive model (`motion.py`), so a good episode is kinematically consistent
by construction: wheel velocities equal `(v ± wL/2)/r` exactly, IMU acceleration is
`dv/dt` forward and `v·w` lateral, and the TF pose is the integral of the odom twist.
That is what makes the implemented AMR plausibility checks testable against
known ground truth.

## Encoding: rosbags → raw CDR → `mcap` writer

| Package | Version | License |
|---|---|---|
| [`rosbags`](https://gitlab.com/ternaris/rosbags) (Ternaris) | 0.10.11 | Apache-2.0 |
| `Pillow` | 12.3.0 | MIT-CMU |
| `mcap` (already a repo dependency) | 1.4.0 | MIT |

`rosbags` ships the ROS 2 Humble type definitions as pure-Python dataclasses plus a CDR
serializer, so the dev image stays a plain `python:3.12-slim` with no ROS install. We
feed those raw CDR bytes to the `mcap` writer directly rather than using rosbags'
rosbag2 writer, because the pipeline ingests single `.mcap` files, not rosbag2
directories.

**The acceptance bar is not "rosbags can read it back."** It is that the bytes decode
through the processor's real schema-bound `mcap-ros2-support` path, in the
`robot-vault-processor` image:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/tmp_simgen:/fixtures" -v "$PWD/development:/development" \
  processor python3 /development/sim_gen/verify_cdr.py \
    /fixtures/sim_sim-amr-01_seed42.mcap \
    --sidecar /fixtures/sim_sim-amr-01_seed42.sim.json
```

`verify_cdr.py` asserts, for every message: `message_encoding == 'cdr'`, schema encoding
`ros2msg`, the schema name is in the processor allowlist, `processor.cdr_decode.try_decode_cdr`
returns a message, required fields are non-null, and `processor.extractors.extract_xyz`
yields non-null x/y/z for Imu/Twist/Odometry/NavSatFix.

## Determinism

Output is a pure function of the arguments — verified byte-identical across separate
container runs. Nothing reads the wall clock (timestamps start at a fixed epoch,
`--start-time-ns`) or the OS entropy pool (every draw comes from
`numpy.random.default_rng([seed, ...])`). `rosbags` and `Pillow` are pinned exactly in
`requirements-dev.txt` for the same reason: the CDR encoder and the JPEG encoder are
both part of those bytes. The guarantee is scoped to a given dev-image build — the
`mcap` container writer and its chunk compressor contribute bytes too, and `mcap` is
still a floor (`mcap>=1.2.1`) because the phase packages share that requirement. Use
`--compression none` if you ever need output independent of the compressor.

## `--bad-episode K`

Episode K (1-based) is degraded in five specific, documented ways. Every window below
is episode-relative and recorded verbatim in the sidecar, so QA tests assert against
ground truth rather than magic numbers:

| Sidecar `kind` | Window (fraction of episode duration) | What is wrong |
|---|---|---|
| `imu_dropout` | `[0.10·d, 0.10·d + 5 s)` | no `/imu/data` messages at all; every other topic keeps publishing, so the *global* silence never reaches the 5 s segmenter threshold |
| `camera_frozen` | `[0.50·d, 0.50·d + 3 s)` | byte-identical JPEG payloads (headers keep advancing) while the robot keeps moving |
| `camera_blur` | `[0.72·d, 0.92·d)` | Gaussian blur, radius 4 px, before JPEG encoding — drops the Laplacian variance by ~2 orders of magnitude |
| `kinematic_implausibility` | `[0.72·d, 0.95·d)` | `/cmd_vel` keeps commanding motion while `/odom`, `/joint_states` and `/tf` report zero velocity and a frozen pose |
| `missing_camera_info` | whole episode | no `sensor_msgs/msg/CameraInfo` at all — calibration incompleteness |

At the default durations (15–20 s) only `camera_blur` and `kinematic_implausibility`
overlap, and those two are independent signals. Shorter episodes clip the windows; a
window clipped to nothing is neither applied nor recorded.

## Sidecar (`<name>.sim.json`)

Records the generator/sidecar versions, the seed and every argument, the mcap sha256,
the exact `regenerate_command`, the topic/rate table, and per episode: index,
`task_label`, first/last `log_time`, per-topic message counts, and the `degradations`
list above. `rsm scan` globs `*.mcap` only, so the sidecar never gets uploaded — it
stays on the robot side as ground truth.

## Tests

These generator tests are a focused development-tooling suite rather than part
of `make test-unit`; see [the repository test matrix](../../docs/TESTING.md).

```bash
docker compose run --rm dev pytest development/sim_gen/tests -q
```
