# manipulator-pickplace example datacard

- **Robot ID:** `example-manipulator-01`
- **Robot type:** bimanual ALOHA manipulator
- **MCAP:** `manipulator-pickplace.mcap` (55,332 bytes)
- **SHA-256:** `48c41711e8fea121af012df9abab373f9f433ea2a528354d7812eaa1093263af`
- **Encoding:** ROS 2 CDR payloads with `ros2msg` schemas in MCAP

## What it demonstrates

- LeRobot-native Parquet -> MCAP ingest -> LeRobot export round-trip provenance
- full 14-D observed joint state, effort, and action target streams
- typed Twist/action provenance and the recorded-but-excluded manipulator-shaped QA subscores

## What to inspect

- Explore: observation and action JointState topics
- Episodes QA detail: commodity excluded_subscores remains explicit
- Sidecar projection warning before interpreting Twist/Odometry as mobile motion

## Source, license, and attribution

- **Dataset:** LeRobot aloha_static_coffee
- **Source Url:** https://huggingface.co/datasets/lerobot/aloha_static_coffee
- **Revision:** b144896feb1f37398a862927b22cd3abdf005a6b
- **License:** MIT
- **Attribution:** LeRobot aloha_static_coffee dataset (ALOHA; Tony Zhao, Vikash Kumar, Sergey Levine, and Chelsea Finn), MIT License.
- **Source Sha256:** ed62be90cd794f6672b26485be2b01af47409ba4cd31042fe261ddc79090bcc1

## Transformations

- selected the first eight seconds of source episode 0 and sampled 50 Hz rows at 25 Hz
- preserved all 14 source state/action dimensions as sensor_msgs/JointState
- added a namespaced, reversible two-waist-velocity projection into Twist/Odometry for current AMR-oriented metrics

## Notes and limitations

- The projection is not mobile-base motion and is never described as such.
- No source video is redistributed in this compact excerpt; camera fields are absent by design.
