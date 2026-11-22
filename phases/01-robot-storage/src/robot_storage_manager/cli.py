import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from robot_vault_config.model import load_config
from robot_vault_config.upload_status import (
	PERSISTED_UPLOAD_STATUSES,
	parse as parse_status,
)
from .core import (
	scan_for_data_files,
	load_or_create_manifest,
	save_manifest,
	is_file_stable,
	update_manifest_sha256,
	delete_retention_item,
	DEFAULT_STABILITY_SECONDS,
	RetentionPlanner,
	_is_valid_sha256,
	_validate_extension,
)


def _print(obj) -> None:
	print(json.dumps(obj, indent=2, sort_keys=True))


def _existing_directory(value: str) -> Path:
	try:
		path = Path(value).expanduser().resolve(strict=True)
	except OSError as exc:
		raise argparse.ArgumentTypeError(f"directory does not exist: {value}") from exc
	if not path.is_dir():
		raise argparse.ArgumentTypeError(f"not a directory: {value}")
	return path


def _existing_regular_file(value: str) -> Path:
	path = Path(value).expanduser()
	if path.is_symlink():
		raise argparse.ArgumentTypeError(f"data-file symlinks are not accepted: {value}")
	try:
		path = path.resolve(strict=True)
	except OSError as exc:
		raise argparse.ArgumentTypeError(f"file does not exist: {value}") from exc
	if not path.is_file():
		raise argparse.ArgumentTypeError(f"not a regular file: {value}")
	return path


def _extension(value: str) -> str:
	try:
		return _validate_extension(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError(str(exc)) from exc


def _retain_days(value: str) -> int:
	try:
		parsed = int(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError("must be an integer") from exc
	if not 0 <= parsed <= 36500:
		raise argparse.ArgumentTypeError("must be between 0 and 36500")
	return parsed


def _positive_finite(value: str) -> float:
	try:
		parsed = float(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError("must be a number") from exc
	if not math.isfinite(parsed) or not 0 < parsed <= 1_000_000:
		raise argparse.ArgumentTypeError("must be finite, greater than zero, and at most 1000000")
	return parsed


def _non_negative_finite(value: str) -> float:
	try:
		parsed = float(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError("must be a number") from exc
	if not math.isfinite(parsed) or parsed < 0:
		raise argparse.ArgumentTypeError("must be finite and non-negative")
	return parsed


def _aware_rfc3339(value: str) -> str:
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError as exc:
		raise argparse.ArgumentTypeError("must be an RFC3339 timestamp") from exc
	if parsed.tzinfo is None or parsed.utcoffset() is None:
		raise argparse.ArgumentTypeError("must include a timezone offset")
	return parsed.isoformat()


def _resolved_extension(args: argparse.Namespace, cfg) -> str:
	return _validate_extension(args.ext or (cfg.paths.extension if cfg else ".mcap"))


def cmd_scan(args: argparse.Namespace) -> int:
	root = Path(args.root)
	cfg = load_config(Path(args.config)) if args.config else None
	ext = _resolved_extension(args, cfg)
	files = scan_for_data_files(root, extension=ext, fail_on_error=True)
	created = 0
	failed = []
	for f in files:
		try:
			manifest = load_or_create_manifest(f, cfg=cfg, root=root)
			save_manifest(manifest, root=root)
			created += 1
		except (OSError, ValueError, RuntimeError) as exc:
			failed.append({"path": str(f), "error": str(exc)})
	_print({"files_scanned": len(files), "manifests_written": created, "failed": failed})
	return 1 if failed else 0


def cmd_hash(args: argparse.Namespace) -> int:
	root = Path(args.root)
	cfg = load_config(Path(args.config)) if args.config else None
	missing_only = args.missing_only
	ext = _resolved_extension(args, cfg)
	min_stable_age = (
		args.min_stable_age_seconds
		if args.min_stable_age_seconds is not None
		else (
			float(cfg.upload_agent.min_stable_age_seconds)
			if cfg is not None
			else DEFAULT_STABILITY_SECONDS
		)
	)
	files = scan_for_data_files(root, extension=ext, fail_on_error=True)
	updated = 0
	skipped_unstable = []
	failed = []
	for f in files:
		try:
			manifest = load_or_create_manifest(f, cfg=cfg, root=root)
			if missing_only and _is_valid_sha256(manifest.get("sha256_hash")):
				continue
			if not is_file_stable(f, min_age_seconds=min_stable_age):
				skipped_unstable.append(str(f))
				continue
			update_manifest_sha256(f, manifest, root=root)
			save_manifest(manifest, root=root)
			updated += 1
		except (OSError, ValueError, RuntimeError) as exc:
			failed.append({"path": str(f), "error": str(exc)})
	_print({
		"files_hashed": updated,
		"files_skipped_unstable": skipped_unstable,
		"failed": failed,
	})
	return 1 if failed else 0


def cmd_list(args: argparse.Namespace) -> int:
	root = Path(args.root)
	cfg = load_config(Path(args.config)) if args.config else None
	ext = _resolved_extension(args, cfg)
	# Compare canonical values, so case and whitespace do not matter.
	status_filter = parse_status(args.status)
	files = scan_for_data_files(root, extension=ext, fail_on_error=True)
	items = []
	for f in files:
		mf = load_or_create_manifest(f, cfg=cfg, root=root)
		if status_filter and parse_status(mf.get("status")) is not status_filter:
			continue
		items.append({
			"path": str(f),
			"size": mf.get("file_size_bytes"),
			"status": mf.get("status"),
			"sha256": mf.get("sha256_hash"),
		})
	_print(items)
	return 0


def cmd_inspect(args: argparse.Namespace) -> int:
	p = Path(args.path)
	mf = load_or_create_manifest(p)
	_print(mf)
	return 0


def _build_retention_planner(args: argparse.Namespace, cfg) -> RetentionPlanner:
	# Precedence: explicit CLI flag > shared config > RetentionPlanner dataclass default.
	# argparse defaults for --min-retain-days/--max-total-gib are None so we can
	# tell "operator passed this" apart from "operator didn't mention it".
	return RetentionPlanner.from_config(
		cfg,
		now_override=args.now,
		min_retain_days=args.min_retain_days,
		max_total_gib=args.max_total_gib,
		allow_unuploaded=bool(getattr(args, "allow_unuploaded", False)),
	)


def cmd_retention_plan(args: argparse.Namespace) -> int:
	root = Path(args.root)
	cfg = load_config(Path(args.config)) if args.config else None
	ext = _resolved_extension(args, cfg)
	files = scan_for_data_files(root, extension=ext, fail_on_error=True)
	manifests = [load_or_create_manifest(f, cfg=cfg, root=root) for f in files]
	planner = _build_retention_planner(args, cfg)
	plan = planner.plan(manifests, root=root)
	_print(plan)
	return 0


def cmd_retention_apply(args: argparse.Namespace) -> int:
	root = Path(args.root)
	cfg = load_config(Path(args.config)) if args.config else None
	ext = _resolved_extension(args, cfg)
	files = scan_for_data_files(root, extension=ext, fail_on_error=True)
	manifests = [load_or_create_manifest(f, cfg=cfg, root=root) for f in files]
	planner = _build_retention_planner(args, cfg)
	plan = planner.plan(manifests, root=root)
	at_risk_summary = {
		"never_uploaded_at_risk_count": plan["never_uploaded_at_risk_count"],
		"never_uploaded_at_risk_bytes": plan["never_uploaded_at_risk_bytes"],
		"allow_unuploaded": plan["allow_unuploaded"],
	}
	if not args.execute:
		_print({"dry_run": True, **plan})
		return 0
	# Execute deletions
	bytes_freed = 0
	deleted = 0
	failed = []
	for item in plan["to_delete"]:
		try:
			bytes_freed += delete_retention_item(root, item)
			deleted += 1
		except Exception as e:
			failed.append({"path": str(item.get("path", "<unknown>")), "error": str(e)})
			print(json.dumps({"error": str(e), "path": item.get("path")}), file=sys.stderr)
	projected_total_bytes_after_apply = max(plan["total_bytes"] - bytes_freed, 0)
	capacity_shortfall_bytes_after_apply = max(
		projected_total_bytes_after_apply - plan["limit_bytes"], 0
	)
	capacity_target_met_after_apply = (
		not failed
		and plan["measurement_complete"]
		and capacity_shortfall_bytes_after_apply == 0
	)
	_print({
		"dry_run": False,
		"deleted": deleted,
		"failed": failed,
		"bytes_freed": bytes_freed,
		"planned_bytes_freed": plan["bytes_freed"],
		"total_bytes_before_apply": plan["total_bytes"],
		"projected_total_bytes_after_apply": projected_total_bytes_after_apply,
		"limit_bytes": plan["limit_bytes"],
		"capacity_shortfall_bytes_after_apply": capacity_shortfall_bytes_after_apply,
		"capacity_target_met_after_apply": capacity_target_met_after_apply,
		"measurement_complete": plan["measurement_complete"],
		"protected_uploading": plan["protected_uploading"],
		"protected_by_min_age": plan["protected_by_min_age"],
		"protected_hardlinks": plan["protected_hardlinks"],
		"unverified_completed": plan["unverified_completed"],
		"unreadable": plan["unreadable"],
		**at_risk_summary,
	})
	return 1 if failed or not capacity_target_met_after_apply else 0


def build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(prog="rsm", description="Robot Storage Manager")
	sub = p.add_subparsers(dest="cmd", required=True)

	sp = sub.add_parser("scan", help="Scan for data files and write manifests")
	sp.add_argument("--root", required=True, type=_existing_directory)
	sp.add_argument("--ext", default=None, type=_extension)
	sp.add_argument("--config", default=None, help="Path to shared Vault config (JSON)")
	sp.set_defaults(func=cmd_scan)

	sp = sub.add_parser("hash", help="Compute SHA256 and update manifests")
	sp.add_argument("--root", required=True, type=_existing_directory)
	sp.add_argument("--ext", default=None, type=_extension)
	sp.add_argument("--missing-only", action="store_true")
	sp.add_argument(
		"--min-stable-age-seconds",
		type=_non_negative_finite,
		default=None,
		help="Skip newer files (default: config upload_agent value, otherwise 5 seconds)",
	)
	sp.add_argument("--config", default=None)
	sp.set_defaults(func=cmd_hash)

	sp = sub.add_parser("list", help="List files and manifest summaries")
	sp.add_argument("--root", required=True, type=_existing_directory)
	sp.add_argument("--ext", default=None, type=_extension)
	# Canonical UploadStatus values.
	sp.add_argument(
		"--status",
		choices=[s.value for s in PERSISTED_UPLOAD_STATUSES],
		default=None,
	)
	sp.add_argument("--config", default=None)
	sp.set_defaults(func=cmd_list)

	sp = sub.add_parser("inspect", help="Show a single manifest")
	sp.add_argument("path", type=_existing_regular_file)
	sp.set_defaults(func=cmd_inspect)

	sp = sub.add_parser("retention", help="Retention operations")
	sub2 = sp.add_subparsers(dest="subcmd", required=True)
	sp2 = sub2.add_parser("plan", help="Show retention plan")
	for spp in (sp2,):
		spp.add_argument("--root", required=True, type=_existing_directory)
		spp.add_argument("--ext", default=None, type=_extension)
		# Defaults are None (not 7 / 200.0) so cmd_retention_plan can tell
		# "operator explicitly passed this flag" apart from "didn't mention
		# it", which is required to honor CLI > config > dataclass-default
		# precedence instead of the CLI flag always winning as a hardcoded 7/200.
		spp.add_argument("--min-retain-days", type=_retain_days, default=None)
		spp.add_argument("--max-total-gib", type=_positive_finite, default=None)
		spp.add_argument("--now", type=_aware_rfc3339, default=None, help="Override now (RFC3339) for tests")
		spp.add_argument("--config", default=None)
		spp.add_argument(
			"--allow-unuploaded",
			action="store_true",
			help="Allow never-uploaded (pending/failed) files to appear in the deletion plan. "
			"Off by default: never-uploaded files are protected and only reported as at-risk.",
		)
	sp2.set_defaults(func=cmd_retention_plan)

	sp2 = sub2.add_parser("apply", help="Apply retention plan (use --execute to actually delete)")
	for spp in (sp2,):
		spp.add_argument("--root", required=True, type=_existing_directory)
		spp.add_argument("--ext", default=None, type=_extension)
		spp.add_argument("--min-retain-days", type=_retain_days, default=None)
		spp.add_argument("--max-total-gib", type=_positive_finite, default=None)
		spp.add_argument("--now", type=_aware_rfc3339, default=None)
		spp.add_argument("--execute", action="store_true")
		spp.add_argument("--config", default=None)
		spp.add_argument(
			"--allow-unuploaded",
			action="store_true",
			help="Allow never-uploaded (pending/failed) files to be deleted. "
			"Off by default: never-uploaded files are protected and only reported as at-risk.",
		)
	sp2.set_defaults(func=cmd_retention_apply)

	return p


def main(argv: Optional[List[str]] = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)
	try:
		return args.func(args)
	except (OSError, ValueError, RuntimeError) as exc:
		print(json.dumps({"error": str(exc), "command": args.cmd}), file=sys.stderr)
		return 2


if __name__ == "__main__":
	sys.exit(main())
