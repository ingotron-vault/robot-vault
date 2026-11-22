# Phase 01 — Robot storage user guide

This guide is for operators who need to inspect recordings, compute hashes, or
free local disk space with the robot storage manager (`rsm`).

## What this does
- Organizes files by robot and date, like `/robot_data/rover-7/2025-08-14/*.mcap`.
- Creates a small “manifest” file next to each data file with its size and integrity hash.
- Plans safe cleanup when disk is getting full:
  - First deletes files whose completed upload has a hash and upload ID
  - Never-uploaded files (pending/failed) are protected by default and are only deleted if you pass `--allow-unuploaded`
  - Never deletes files currently uploading
  - Protects completed files whose upload evidence is incomplete and reports
    them as `unverified_completed`

## Common tasks
- List files and statuses
- Compute integrity hashes
- Plan cleanups (dry-run by default)

## Example commands

1. Scan recordings and create manifests:

   ```bash
   rsm scan --root /robot_data/rover-7
   ```

2. Compute hashes for files missing them:

   ```bash
   rsm hash --root /robot_data/rover-7 --missing-only
   ```

   Files modified in the last five seconds are reported as unstable and
   skipped. A `.mcap` file without a complete MCAP footer is also skipped,
   because the recorder writes the footer last. Wait for recording to finish.
   `--min-stable-age-seconds 0` turns off only the five-second check; the
   footer check always applies.

3. Show one manifest:

   ```bash
   rsm inspect /robot_data/rover-7/2025-08-14/drive_001.mcap
   ```

4. Plan cleanup to keep at least 14 days and under 200 GiB:

   ```bash
   rsm retention plan --root /robot_data/rover-7 --min-retain-days 14 --max-total-gib 200
   ```

5. Read the plan, then apply it if the proposed deletions are correct. This
   command deletes files:

   ```bash
   rsm retention apply --root /robot_data/rover-7 --min-retain-days 14 --max-total-gib 200 --execute
   ```

You can repeat scan, hash, and inspect to refresh or read the current state.
Review each retention plan before applying it; a later plan can select more
files for deletion. Without `--execute`, no files are deleted.

`capacity_target_met: false` in the retention JSON means
the cap cannot be reached without protected data. An executed apply also
returns a non-zero exit code if a file/manifest changed after planning or if
the target remains unmet; it never silently reports that stale plan as a full
success.
