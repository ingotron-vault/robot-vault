# imperfect-run example datacard

- **Robot ID:** `example-wheeled-01`
- **Robot type:** Pioneer mobile robot (engineered QA-fault derivative)
- **MCAP:** `imperfect-run.mcap` (214,866 bytes)
- **SHA-256:** `a21dead075e4598844e584aac13d641bce13321c10343aaa87559481d4442511`
- **Encoding:** ROS 2 CDR payloads with `ros2msg` schemas in MCAP

## What it demonstrates

- real QA DEFECT signals from missing calibration, timestamp jitter, and stream gaps
- real QA RISK signals from an unlabeled episode and frozen/blurred frames
- checks flag data without deleting it

## What to inspect

- Episodes QA breakdown: both DEFECT and RISK classes
- cross_stream_sync interpolation provenance
- calibration, completeness, image_quality, and labels reasons

## Source, license, and attribution

- **Dataset:** TUM RGB-D Benchmark derivative
- **Sequence:** fr2/pioneer_slam2
- **Source Url:** https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Attribution:** Technical University of Munich, Computer Vision Group, TUM RGB-D Benchmark, sequence fr2/pioneer_slam2, CC BY 4.0.
- **Source Sha256:** 7e8f31ec7d1893f6bd7eaaae5092bc186957c9ff0eb0a85cdca404220ebcd37d

## Transformations

- started from the exact wheeled-nominal source window
- removed CameraInfo and static TF, dropped camera/laser/TF stretches, jittered camera header stamps by +/-850 ms
- froze and blurred a camera interval and emitted a deliberately empty task marker
- did not insert, update, or otherwise fabricate any qa_reports row

## Notes and limitations

- This bag must be accepted only if the real processor reports at least one low DEFECT and one low RISK check.
- Malformed/truncated CDR was intentionally avoided so every retained message remains decodable.
