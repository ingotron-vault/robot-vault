# wheeled-nominal example datacard

- **Robot ID:** `example-wheeled-01`
- **Robot type:** Pioneer mobile robot
- **MCAP:** `wheeled-nominal.mcap` (509,376 bytes)
- **SHA-256:** `bf17ae1eeaa6c5b27c842b68d8a2f4336b5e7c18c16dba94263426bf2a1703ef`
- **Encoding:** ROS 2 CDR payloads with `ros2msg` schemas in MCAP

## What it demonstrates

- clean nominal upload -> episode -> QA path
- real camera, laser, wheel odometry, and measured Twist data
- real non-zero angular_z preserved in the typed v2 Twist family

## What to inspect

- Episodes QA: strongest clean example
- Explore: odometry, laser_scan, compressed_image, and twist families
- Datasets: use this episode for the pre-populated signed example cut

## Source, license, and attribution

- **Dataset:** TUM RGB-D Benchmark
- **Sequence:** fr2/pioneer_slam2
- **Source Url:** https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Attribution:** Technical University of Munich, Computer Vision Group, TUM RGB-D Benchmark, sequence fr2/pioneer_slam2, CC BY 4.0.
- **Source Sha256:** 7e8f31ec7d1893f6bd7eaaae5092bc186957c9ff0eb0a85cdca404220ebcd37d

## Transformations

- selected one contiguous eight-second window containing a real Pioneer turn
- derived measured linear/angular velocity from the real mocap-backed /pose trajectory because the source carries no command topic
- rebased timestamps to a deterministic epoch without changing relative timing
- decoded ROS 1 messages and re-encoded equivalent ROS 2 CDR messages in MCAP
- sampled RGB at 5 Hz and resized to 480x360; scaled source intrinsics accordingly

## Notes and limitations

- CC BY 4.0 attribution must remain with this derivative.
- The QA calibration check proves artifact presence, not calibration accuracy.
