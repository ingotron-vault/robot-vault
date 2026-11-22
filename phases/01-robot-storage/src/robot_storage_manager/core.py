import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

from robot_vault_config.model import VaultConfig
from robot_vault_config.upload_status import UploadStatus, parse as parse_status

MANIFEST_SUFFIX = ".manifest.json"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_STABILITY_SECONDS = 5.0
DEFAULT_STALE_UPLOADING_SECONDS = 3600.0
# MCAP layout at end of file: Footer record, then the closing magic.
# Footer = opcode 0x02, record length 20 (u64 LE), summary_start (u64),
# summary_offset_start (u64), summary_crc (u32).
MCAP_MAGIC = b"\x89MCAP0\r\n"
MCAP_FOOTER_HEADER = b"\x02" + (20).to_bytes(8, "little")
MCAP_FOOTER_HEADER_BYTES = len(MCAP_FOOTER_HEADER)
MCAP_TAIL_BYTES = MCAP_FOOTER_HEADER_BYTES + 20 + len(MCAP_MAGIC)
# Leading magic plus the footer and closing magic.
MCAP_MIN_COMPLETE_BYTES = len(MCAP_MAGIC) + MCAP_TAIL_BYTES
MAX_MANIFEST_BYTES = 1024 * 1024
CORRUPT_COPY_TRUNCATION_MARKER = (
	b"\n... corrupt manifest truncated by robot-storage-manager ...\n"
)
CAPTURE_MANIFEST_KEYS = (
	"operator",
	"site",
	"permission_ref",
	"session_tags",
	"task_label",
)

logger = logging.getLogger(__name__)


class ManifestFormatError(ValueError):
	"""A manifest is syntactically unsafe or outside the supported envelope."""


class ConcurrentManifestUpdateError(RuntimeError):
	"""A caller tried to save a manifest revision older than the on-disk one."""


class FileChangedDuringHashError(OSError):
	"""The data file changed while its digest was being computed."""


def _now_iso(now_override: Optional[str] = None) -> str:
	return now_override or datetime.now(timezone.utc).isoformat()


def _validate_extension(extension: str) -> str:
	if (
		not isinstance(extension, str)
		or len(extension) < 2
		or not extension.startswith(".")
		or extension.endswith(".")
		or any(ch in extension for ch in ("/", "\\", "\x00", "*", "?", "[", "]"))
	):
		raise ValueError("extension must be a literal filename suffix such as .mcap")
	return extension


def _stat_identity(value: os.stat_result) -> Dict[str, int]:
	return {
		"device_id": int(value.st_dev),
		"inode": int(value.st_ino),
		"file_size_bytes": int(value.st_size),
		"mtime_ns": int(value.st_mtime_ns),
		"ctime_ns": int(value.st_ctime_ns),
	}


def _identity_matches(expected: Dict, actual: os.stat_result) -> bool:
	return all(expected.get(key) == value for key, value in _stat_identity(actual).items())


def _same_manifest_content(left: Dict, right: Dict) -> bool:
	if (
		left.get("file_size_bytes") != right.get("file_size_bytes")
		or left.get("mtime_ns") != right.get("mtime_ns")
	):
		return False
	for key in ("device_id", "inode", "ctime_ns"):
		if key in left and key in right and left.get(key) != right.get(key):
			return False
	return True


def _same_manifest_content_strict(left: Dict, right: Dict) -> bool:
	keys = ("file_size_bytes", "mtime_ns", "device_id", "inode", "ctime_ns")
	return all(
		isinstance(left.get(key), int)
		and not isinstance(left.get(key), bool)
		and left.get(key) == right.get(key)
		for key in keys
	)


def _is_valid_sha256(value) -> bool:
	return (
		isinstance(value, str)
		and len(value) == 64
		and all(ch in "0123456789abcdefABCDEF" for ch in value)
	)


def _manifest_revision(manifest: Optional[Dict]) -> int:
	if not manifest:
		return 0
	value = manifest.get("manifest_revision", 0)
	if isinstance(value, bool) or not isinstance(value, int) or value < 0:
		return 0
	return value


def _reject_duplicate_keys(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise ManifestFormatError(f"duplicate manifest key: {key}")
		result[key] = value
	return result


def _reject_non_finite_json(value: str):
	raise ManifestFormatError(f"non-finite JSON number is not permitted: {value}")


def _decode_manifest(raw: bytes) -> Dict:
	try:
		loaded = json.loads(
			raw.decode("utf-8"),
			object_pairs_hook=_reject_duplicate_keys,
			parse_constant=_reject_non_finite_json,
		)
	except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
		raise ManifestFormatError(str(exc)) from exc
	if not isinstance(loaded, dict):
		raise ManifestFormatError("top-level JSON must be an object")
	return loaded


def _read_fd_bytes(file_obj: BinaryIO, maximum: int = MAX_MANIFEST_BYTES) -> bytes:
	raw = file_obj.read(maximum + 1)
	if len(raw) > maximum:
		raise ManifestFormatError(f"manifest exceeds {maximum} bytes")
	return raw


def _read_manifest_raw_at(
	parent_fd: Optional[int], name_or_path
) -> Tuple[bytes, os.stat_result]:
	flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
	fd = (
		os.open(name_or_path, flags)
		if parent_fd is None
		else os.open(name_or_path, flags, dir_fd=parent_fd)
	)
	try:
		before = os.fstat(fd)
		if not stat.S_ISREG(before.st_mode):
			raise OSError(errno.EINVAL, "manifest is not a regular file", str(name_or_path))
		with os.fdopen(fd, "rb", closefd=False) as file_obj:
			raw = _read_fd_bytes(file_obj)
		after = os.fstat(fd)
		if _stat_identity(before) != _stat_identity(after):
			raise OSError(
				errno.ESTALE, "manifest changed while it was being read", str(name_or_path)
			)
		return raw, after
	finally:
		os.close(fd)


def _read_manifest_raw_snapshot(path: Path) -> Tuple[bytes, os.stat_result]:
	return _read_manifest_raw_at(None, path)


def _open_parent_beneath(root: Path, target: Path) -> int:
	"""Open target's parent without following any directory symlink."""
	root = root.resolve(strict=True)
	if not root.is_dir():
		raise NotADirectoryError(str(root))
	if not target.is_absolute() or Path(os.path.normpath(str(target))) != target:
		raise ValueError("target must be a normalized absolute path")
	try:
		relative = target.relative_to(root)
	except ValueError as exc:
		raise ValueError("target escapes the configured root") from exc
	flags = (
		os.O_RDONLY
		| getattr(os, "O_DIRECTORY", 0)
		| getattr(os, "O_CLOEXEC", 0)
		| getattr(os, "O_NOFOLLOW", 0)
	)
	current_fd = os.open(root, flags)
	try:
		for component in relative.parts[:-1]:
			next_fd = os.open(component, flags, dir_fd=current_fd)
			os.close(current_fd)
			current_fd = next_fd
		return current_fd
	except BaseException:
		os.close(current_fd)
		raise


def scan_for_data_files(
	root: Path, extension: str = ".mcap", fail_on_error: bool = False
) -> List[Path]:
	"""Return non-symlink regular data files physically contained by root."""
	extension = _validate_extension(extension)
	try:
		resolved_root = root.resolve(strict=True)
	except OSError:
		if fail_on_error:
			raise
		return []
	files: List[Path] = []
	try:
		for path in root.rglob("*"):
			try:
				if (
					not path.name.endswith(extension)
					or path.is_symlink()
					or not path.is_file()
				):
					continue
				resolved = path.resolve(strict=True)
				if not resolved.is_relative_to(resolved_root):
					logger.warning("Skipping data file outside capture root: %s", path)
					continue
				files.append(resolved)
			except OSError:
				if fail_on_error:
					raise
				logger.warning("Skipping unreadable data file %s", path, exc_info=True)
	except OSError:
		if fail_on_error:
			raise
		logger.warning("Could not scan %s for data files", root, exc_info=True)
	return sorted(files)


def manifest_path_for(data_file: Path) -> Path:
	return data_file.with_suffix(data_file.suffix + MANIFEST_SUFFIX)


def has_complete_mcap_footer(path: Path) -> bool:
	"""Does ``path`` end with an MCAP Footer record and the closing magic?

	A recorder writes the footer and the closing magic last. Without them the
	file is still being written, even when its mtime has not changed for a
	while (a buffered recorder can pause for longer than the mtime window).
	An unreadable file returns True so the caller reports the real error.
	"""
	try:
		with open(path, "rb") as handle:
			handle.seek(0, os.SEEK_END)
			if handle.tell() < MCAP_MIN_COMPLETE_BYTES:
				return False
			handle.seek(-MCAP_TAIL_BYTES, os.SEEK_END)
			tail = handle.read(MCAP_TAIL_BYTES)
	except OSError:
		return True
	return (
		tail[-len(MCAP_MAGIC):] == MCAP_MAGIC
		and tail[:MCAP_FOOTER_HEADER_BYTES] == MCAP_FOOTER_HEADER
	)


def is_file_stable(
	path: Path,
	min_age_seconds: float = DEFAULT_STABILITY_SECONDS,
	now: Optional[float] = None,
) -> bool:
	"""Is ``path`` finished, and so safe to hash and upload?

	Two checks. The mtime must be at least ``min_age_seconds`` old (0 turns
	this check off). A ``.mcap`` file must also end with a complete MCAP
	footer; this check is always on. A file that fails either check is still
	being written: callers skip it and try again on a later scan.
	"""
	try:
		mtime = path.stat().st_mtime
	except OSError:
		return True
	if min_age_seconds > 0:
		current = now if now is not None else datetime.now(timezone.utc).timestamp()
		if current - mtime < min_age_seconds:
			return False
	if path.name.lower().endswith(".mcap"):
		return has_complete_mcap_footer(path)
	return True


def _reconcile_stale_uploading(
	loaded: Dict,
	mf_path: Path,
	stale_uploading_seconds: float = DEFAULT_STALE_UPLOADING_SECONDS,
	now: Optional[float] = None,
	manifest_mtime: Optional[float] = None,
) -> None:
	if parse_status(loaded.get("status")) is not UploadStatus.UPLOADING:
		return
	if manifest_mtime is None:
		try:
			manifest_mtime = mf_path.stat().st_mtime
		except OSError:
			return
	current = now if now is not None else datetime.now(timezone.utc).timestamp()
	if current - manifest_mtime > stale_uploading_seconds:
		logger.warning(
			"Manifest %s has been uploading for over %.0fs; resetting it to failed",
			mf_path,
			stale_uploading_seconds,
		)
		loaded["status"] = UploadStatus.FAILED.value


def _preserve_corrupt_manifest(
	mf_path: Path,
	raw: Optional[bytes] = None,
	parent_fd: Optional[int] = None,
) -> None:
	corrupt_path = mf_path.with_name(mf_path.name + ".corrupt")
	flags = (
		os.O_WRONLY
		| os.O_CREAT
		| os.O_EXCL
		| getattr(os, "O_CLOEXEC", 0)
		| getattr(os, "O_NOFOLLOW", 0)
	)
	try:
		fd = (
			os.open(corrupt_path, flags, 0o600)
			if parent_fd is None
			else os.open(corrupt_path.name, flags, 0o600, dir_fd=parent_fd)
		)
		try:
			if raw is None:
				source_flags = (
					os.O_RDONLY
					| getattr(os, "O_CLOEXEC", 0)
					| getattr(os, "O_NOFOLLOW", 0)
				)
				source_fd = (
					os.open(mf_path, source_flags)
					if parent_fd is None
					else os.open(mf_path.name, source_flags, dir_fd=parent_fd)
				)
				try:
					with os.fdopen(source_fd, "rb", closefd=False) as source:
						raw = source.read(MAX_MANIFEST_BYTES)
						if source.read(1):
							raw += CORRUPT_COPY_TRUNCATION_MARKER
				finally:
					os.close(source_fd)
			with os.fdopen(fd, "wb", closefd=False) as destination:
				destination.write(raw)
				destination.flush()
				os.fsync(destination.fileno())
		finally:
			os.close(fd)
	except FileExistsError:
		logger.warning("Corrupt-manifest diagnostic already exists at %s", corrupt_path)
	except OSError:
		logger.exception("Failed to preserve corrupt manifest copy for %s", mf_path)


def load_or_create_manifest(
	data_file: Path,
	stale_uploading_seconds: float = DEFAULT_STALE_UPLOADING_SECONDS,
	now: Optional[float] = None,
	cfg: Optional[VaultConfig] = None,
	root: Optional[Path] = None,
) -> Dict:
	"""Load live-file metadata or build a pending record.

	When root is supplied, all access is descriptor-anchored beneath it.
	"""
	data_file = data_file.expanduser()
	if data_file.is_symlink():
		raise OSError(errno.ELOOP, "data path must not be a symlink", str(data_file))
	data_file = data_file.resolve(strict=True)
	anchor = root.resolve(strict=True) if root is not None else Path(data_file.anchor)
	if root is not None and not data_file.is_relative_to(anchor):
		raise ValueError("data path escapes the configured capture root")
	parent_fd = _open_parent_beneath(anchor, data_file)
	try:
		data_stat = os.stat(data_file.name, dir_fd=parent_fd, follow_symlinks=False)
		if not stat.S_ISREG(data_stat.st_mode):
			raise OSError(errno.EINVAL, "data path is not a regular file", str(data_file))
		identity = _stat_identity(data_stat)
		capture = getattr(cfg, "capture", None)
		base = {
			"path": str(data_file),
			**identity,
			"sha256_hash": None,
			"created_at": _now_iso(),
			"recorded_at": None,
			"status": UploadStatus.PENDING.value,
			"upload_id": None,
			"labels": None,
			"manifest_revision": 0,
			**{key: getattr(capture, key, None) for key in CAPTURE_MANIFEST_KEYS},
		}
		mf_path = manifest_path_for(data_file)
		try:
			if root is None:
				raw, manifest_stat = _read_manifest_raw_snapshot(mf_path)
			else:
				raw, manifest_stat = _read_manifest_raw_at(parent_fd, mf_path.name)
			loaded = _decode_manifest(raw)
		except FileNotFoundError:
			return base
		except OSError:
			raise
		except ManifestFormatError:
			logger.warning(
				"Corrupt manifest at %s; preserving a diagnostic and regenerating pending",
				mf_path,
			)
			_preserve_corrupt_manifest(
				mf_path, locals().get("raw"), parent_fd=parent_fd
			)
			return base
	finally:
		os.close(parent_fd)

	loaded["path"] = str(data_file)
	loaded["manifest_revision"] = _manifest_revision(loaded)
	if not _same_manifest_content(loaded, identity):
		loaded["status"] = UploadStatus.PENDING.value
		loaded["sha256_hash"] = None
		loaded["upload_id"] = None
	_reconcile_stale_uploading(
		loaded,
		manifest_path_for(data_file),
		stale_uploading_seconds,
		now,
		manifest_mtime=manifest_stat.st_mtime,
	)
	loaded.update(identity)
	return loaded


def _open_verified_data_parent(data_path: Path, root: Optional[Path] = None) -> int:
	if not data_path.is_absolute():
		raise ValueError("manifest path must be absolute")
	if data_path.is_symlink() or data_path.resolve(strict=True) != data_path:
		raise OSError(errno.ELOOP, "manifest data path must be canonical and symlink-free")
	anchor = root.resolve(strict=True) if root is not None else Path(data_path.anchor)
	if root is not None and not data_path.is_relative_to(anchor):
		raise ValueError("manifest data path escapes the configured capture root")
	parent_fd = _open_parent_beneath(anchor, data_path)
	try:
		data_stat = os.stat(data_path.name, dir_fd=parent_fd, follow_symlinks=False)
		if not stat.S_ISREG(data_stat.st_mode):
			raise OSError(errno.EINVAL, "manifest data path is no longer regular")
	except BaseException:
		os.close(parent_fd)
		raise
	return parent_fd


def _read_manifest_at(parent_fd: int, name: str) -> Dict:
	raw, _ = _read_manifest_raw_at(parent_fd, name)
	return _decode_manifest(raw)


def save_manifest(manifest: Dict, root: Optional[Path] = None) -> None:
	"""Atomically and durably save one revision under a cross-process lock."""
	if not isinstance(manifest, dict) or not isinstance(manifest.get("path"), str):
		raise ValueError("manifest.path must be an absolute data-file path")
	data_path = Path(manifest["path"])
	mf_path = manifest_path_for(data_path)
	parent_fd = _open_verified_data_parent(data_path, root=root)
	lock_fd = -1
	tmp_name: Optional[str] = None
	try:
		lock_flags = (
			os.O_RDWR
			| os.O_CREAT
			| getattr(os, "O_CLOEXEC", 0)
			| getattr(os, "O_NOFOLLOW", 0)
		)
		lock_fd = os.open(
			mf_path.name + ".lock", lock_flags, 0o600, dir_fd=parent_fd
		)
		fcntl.flock(lock_fd, fcntl.LOCK_EX)
		try:
			current = _read_manifest_at(parent_fd, mf_path.name)
		except FileNotFoundError:
			current = None
		except ManifestFormatError:
			current = None

		incoming_revision = _manifest_revision(manifest)
		current_revision = _manifest_revision(current)
		outgoing = dict(manifest)
		if current is None:
			if incoming_revision != 0:
				raise ConcurrentManifestUpdateError(
					"manifest disappeared or became unreadable after it was loaded"
				)
		elif incoming_revision != current_revision:
			same_content = _same_manifest_content_strict(current, outgoing)
			current_status = parse_status(
				current.get("status"), UploadStatus.PENDING
			)
			incoming_status = parse_status(
				outgoing.get("status"), UploadStatus.PENDING
			)
			if same_content and current_status is UploadStatus.COMPLETED:
				manifest.clear()
				manifest.update(current)
				return
			incoming_has_evidence = (
				_is_valid_sha256(outgoing.get("sha256_hash"))
				and isinstance(outgoing.get("upload_id"), str)
				and bool(outgoing["upload_id"].strip())
			)
			if not (
				same_content
				and incoming_status is UploadStatus.COMPLETED
				and incoming_has_evidence
			):
				raise ConcurrentManifestUpdateError(
					f"stale manifest revision {incoming_revision}; current revision is "
					f"{current_revision}"
				)
		elif outgoing == current:
			manifest.clear()
			manifest.update(current)
			return

		outgoing["manifest_revision"] = current_revision + 1
		data = json.dumps(
			outgoing, indent=2, sort_keys=True, allow_nan=False
		).encode("utf-8")
		if len(data) > MAX_MANIFEST_BYTES:
			raise ManifestFormatError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
		tmp_name = f"{mf_path.name}.{secrets.token_hex(12)}.tmp"
		tmp_flags = (
			os.O_WRONLY
			| os.O_CREAT
			| os.O_EXCL
			| getattr(os, "O_CLOEXEC", 0)
			| getattr(os, "O_NOFOLLOW", 0)
		)
		tmp_fd = os.open(tmp_name, tmp_flags, 0o600, dir_fd=parent_fd)
		try:
			with os.fdopen(tmp_fd, "wb", closefd=False) as file_obj:
				file_obj.write(data)
				file_obj.flush()
				os.fsync(file_obj.fileno())
		finally:
			os.close(tmp_fd)
		os.replace(
			tmp_name,
			mf_path.name,
			src_dir_fd=parent_fd,
			dst_dir_fd=parent_fd,
		)
		tmp_name = None
		os.fsync(parent_fd)
		manifest.clear()
		manifest.update(outgoing)
	finally:
		if tmp_name is not None:
			try:
				os.unlink(tmp_name, dir_fd=parent_fd)
			except OSError:
				pass
		if lock_fd >= 0:
			try:
				fcntl.flock(lock_fd, fcntl.LOCK_UN)
			finally:
				os.close(lock_fd)
		os.close(parent_fd)


def scan_for_manifests(root: Path) -> List[Path]:
	"""Return every regular manifest sidecar under root without following links."""
	try:
		resolved_root = root.resolve(strict=True)
	except OSError:
		return []
	seen: Dict[Path, None] = {}
	try:
		candidates = root.rglob(f"*{MANIFEST_SUFFIX}")
		while True:
			try:
				path = next(candidates)
			except StopIteration:
				break
			except OSError:
				logger.warning("Skipping unreadable path below %s", root, exc_info=True)
				continue
			try:
				if path.is_symlink() or not path.is_file():
					continue
				resolved = path.resolve(strict=True)
				if not resolved.is_relative_to(resolved_root):
					continue
				seen.setdefault(resolved, None)
			except OSError:
				logger.warning("Skipping unreadable manifest %s", path, exc_info=True)
	except OSError:
		logger.warning("Could not scan %s for manifests", root, exc_info=True)
	return sorted(seen)


def read_manifest(
	manifest_file: Path,
	stale_uploading_seconds: float = DEFAULT_STALE_UPLOADING_SECONDS,
	now: Optional[float] = None,
) -> Optional[Dict]:
	"""Read one manifest for reporting without changing disk state."""
	try:
		raw, manifest_stat = _read_manifest_raw_snapshot(manifest_file)
		loaded = _decode_manifest(raw)
	except (OSError, ManifestFormatError):
		logger.warning("Skipping unreadable manifest at %s", manifest_file)
		return None
	_reconcile_stale_uploading(
		loaded,
		manifest_file,
		stale_uploading_seconds,
		now,
		manifest_mtime=manifest_stat.st_mtime,
	)
	return loaded


def _compute_sha256_and_stat(
	path: Path,
	chunk_size: int = DEFAULT_CHUNK_SIZE,
	root: Optional[Path] = None,
) -> Tuple[str, os.stat_result]:
	if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
		raise ValueError("chunk_size must be a positive integer")
	path = path.expanduser()
	if path.is_symlink():
		raise OSError(errno.ELOOP, "refusing to hash a data-file symlink", str(path))
	path = path.resolve(strict=True)
	anchor = root.resolve(strict=True) if root is not None else Path(path.anchor)
	if root is not None and not path.is_relative_to(anchor):
		raise ValueError("hash target escapes the configured capture root")
	parent_fd = _open_parent_beneath(anchor, path)
	flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
	try:
		fd = os.open(path.name, flags, dir_fd=parent_fd)
	except BaseException:
		os.close(parent_fd)
		raise
	try:
		before = os.fstat(fd)
		if not stat.S_ISREG(before.st_mode):
			raise OSError(errno.EINVAL, "data path is not regular", str(path))
		digest = hashlib.sha256()
		with os.fdopen(fd, "rb", closefd=False) as file_obj:
			while True:
				chunk = file_obj.read(chunk_size)
				if not chunk:
					break
				digest.update(chunk)
		after = os.fstat(fd)
		if _stat_identity(before) != _stat_identity(after):
			raise FileChangedDuringHashError(
				errno.ESTALE,
				"data file changed while its SHA256 was being computed",
				str(path),
			)
		path_now = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
		if _stat_identity(after) != _stat_identity(path_now):
			raise FileChangedDuringHashError(
				errno.ESTALE,
				"data file was replaced while its SHA256 was being computed",
				str(path),
			)
		return digest.hexdigest(), after
	finally:
		os.close(fd)
		os.close(parent_fd)


def compute_sha256_for_file(
	path: Path,
	chunk_size: int = DEFAULT_CHUNK_SIZE,
	root: Optional[Path] = None,
) -> str:
	return _compute_sha256_and_stat(path, chunk_size, root=root)[0]


def update_manifest_sha256(
	path: Path,
	manifest: Dict,
	chunk_size: int = DEFAULT_CHUNK_SIZE,
	root: Optional[Path] = None,
) -> str:
	digest, file_stat = _compute_sha256_and_stat(path, chunk_size, root=root)
	identity = _stat_identity(file_stat)
	if not _same_manifest_content(manifest, identity):
		manifest["status"] = UploadStatus.PENDING.value
		manifest["upload_id"] = None
	manifest.update(identity)
	manifest["sha256_hash"] = digest
	return digest


@dataclass
class RetentionPlanner:
	min_retain_days: int = 7
	max_total_gib: float = 200.0
	now_override: Optional[str] = None
	allow_unuploaded: bool = False

	def __post_init__(self) -> None:
		if (
			isinstance(self.min_retain_days, bool)
			or not isinstance(self.min_retain_days, int)
			or not 0 <= self.min_retain_days <= 36500
		):
			raise ValueError("min_retain_days must be an integer between 0 and 36500")
		if (
			isinstance(self.max_total_gib, bool)
			or not isinstance(self.max_total_gib, (int, float))
			or not math.isfinite(float(self.max_total_gib))
			or not 0 < float(self.max_total_gib) <= 1_000_000
		):
			raise ValueError("max_total_gib must be finite and greater than zero")
		if self.now_override is not None:
			now = datetime.fromisoformat(self.now_override.replace("Z", "+00:00"))
			if now.tzinfo is None or now.utcoffset() is None:
				raise ValueError("now_override must include an RFC3339 timezone offset")
			self.now_override = now.isoformat()

	@staticmethod
	def from_config(
		cfg: Optional[VaultConfig],
		now_override: Optional[str] = None,
		min_retain_days: Optional[int] = None,
		max_total_gib: Optional[float] = None,
		allow_unuploaded: bool = False,
	) -> "RetentionPlanner":
		defaults = RetentionPlanner()
		resolved_days = defaults.min_retain_days
		resolved_gib = defaults.max_total_gib
		if cfg is not None:
			resolved_days = cfg.retention.min_retain_days
			resolved_gib = cfg.retention.max_total_gib
		if min_retain_days is not None:
			resolved_days = min_retain_days
		if max_total_gib is not None:
			resolved_gib = max_total_gib
		return RetentionPlanner(
			min_retain_days=resolved_days,
			max_total_gib=resolved_gib,
			now_override=now_override,
			allow_unuploaded=allow_unuploaded,
		)

	def plan(self, manifests: List[Dict], root: Optional[Path] = None) -> Dict:
		now = (
			datetime.fromisoformat(self.now_override)
			if self.now_override
			else datetime.now(timezone.utc)
		)
		limit_bytes = int(float(self.max_total_gib) * (1024**3))
		resolved_root = root.resolve(strict=True) if root is not None else None
		records: List[Dict] = []
		unreadable: List[Dict] = []
		seen = set()

		for manifest in manifests:
			parent_fd: Optional[int] = None
			try:
				if not isinstance(manifest, dict) or not isinstance(
					manifest.get("path"), str
				):
					raise ValueError("manifest.path is missing or not a string")
				path = Path(manifest["path"])
				if path.is_symlink():
					raise OSError(errno.ELOOP, "data path is a symlink")
				path = path.resolve(strict=True)
				anchor = resolved_root if resolved_root is not None else Path(path.anchor)
				if resolved_root is not None and not path.is_relative_to(resolved_root):
					raise ValueError("manifest path escapes the capture root")
				if path in seen:
					raise ValueError("duplicate manifest path")
				seen.add(path)
				parent_fd = _open_parent_beneath(anchor, path)
				data_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
				if not stat.S_ISREG(data_stat.st_mode):
					raise OSError(errno.EINVAL, "data path is not regular")
				manifest_path = manifest_path_for(path)
				try:
					raw, manifest_stat = _read_manifest_raw_at(
						parent_fd, manifest_path.name
					)
					disk_manifest = _decode_manifest(raw)
					if _manifest_revision(disk_manifest) != _manifest_revision(manifest):
						raise OSError(errno.ESTALE, "manifest changed after it was loaded")
				except FileNotFoundError:
					if _manifest_revision(manifest) != 0:
						raise OSError(errno.ESTALE, "manifest disappeared after it was loaded")
					manifest_stat = None
					disk_manifest = None
				records.append(
					{
						"manifest": manifest,
						"disk_manifest": disk_manifest,
						"path": path,
						"manifest_path": manifest_path,
						"data_stat": data_stat,
						"manifest_stat": manifest_stat,
						"age_days": (now.timestamp() - data_stat.st_mtime) / 86400.0,
						"status": parse_status(
							manifest.get("status"), UploadStatus.PENDING
						),
					}
				)
			except (OSError, ValueError, TypeError) as exc:
				unreadable.append(
					{
						"path": (
							str(manifest.get("path", "<unknown>"))
							if isinstance(manifest, dict)
							else "<unknown>"
						),
						"error": str(exc),
					}
				)
			finally:
				if parent_fd is not None:
					os.close(parent_fd)

		def item(record: Dict) -> Dict:
			manifest = record["manifest"]
			return {
				"path": str(record["path"]),
				"manifest_path": str(record["manifest_path"]),
				"size": int(record["data_stat"].st_size),
				"status": manifest.get("status"),
				"sha256_hash": manifest.get("sha256_hash"),
				"upload_id": manifest.get("upload_id"),
				"manifest_revision": _manifest_revision(manifest),
				"data_identity": _stat_identity(record["data_stat"]),
				"manifest_identity": (
					_stat_identity(record["manifest_stat"])
					if record["manifest_stat"] is not None
					else None
				),
			}

		def verified(record: Dict) -> bool:
			manifest = record["manifest"]
			disk = record["disk_manifest"]
			data_identity = _stat_identity(record["data_stat"])
			return (
				record["status"] is UploadStatus.COMPLETED
				and disk is not None
				and parse_status(disk.get("status")) is UploadStatus.COMPLETED
				and _is_valid_sha256(manifest.get("sha256_hash"))
				and manifest.get("sha256_hash") == disk.get("sha256_hash")
				and isinstance(manifest.get("upload_id"), str)
				and bool(manifest["upload_id"].strip())
				and manifest.get("upload_id") == disk.get("upload_id")
				and _same_manifest_content(manifest, data_identity)
				and _same_manifest_content(disk, data_identity)
			)

		protected_uploading = [
			record for record in records if record["status"] is UploadStatus.UPLOADING
		]
		protected_by_age = [
			record
			for record in records
			if record["status"] is not UploadStatus.UPLOADING
			and record["age_days"] < float(self.min_retain_days)
		]
		old_enough = [
			record
			for record in records
			if record["status"] is not UploadStatus.UPLOADING
			and record["age_days"] >= float(self.min_retain_days)
		]
		protected_hardlinks = [
			record for record in old_enough if record["data_stat"].st_nlink > 1
		]
		eligible = [record for record in old_enough if record["data_stat"].st_nlink == 1]
		primary = sorted(
			(record for record in eligible if verified(record)),
			key=lambda record: record["data_stat"].st_mtime_ns,
		)
		unverified_completed = [
			record
			for record in eligible
			if record["status"] is UploadStatus.COMPLETED and not verified(record)
		]
		secondary = sorted(
			(
				record
				for record in eligible
				if record["status"]
				not in (UploadStatus.COMPLETED, UploadStatus.UPLOADING)
			),
			key=lambda record: record["data_stat"].st_mtime_ns,
		)

		total_bytes = sum(int(record["data_stat"].st_size) for record in records)
		bytes_over = max(total_bytes - limit_bytes, 0)
		to_delete: List[Dict] = []
		at_risk: List[Dict] = []
		planned_freed = 0
		for record in primary:
			if planned_freed >= bytes_over:
				break
			candidate = item(record)
			to_delete.append(candidate)
			planned_freed += candidate["size"]
		if planned_freed < bytes_over:
			needed = bytes_over - planned_freed
			risk_bytes = 0
			for record in secondary:
				candidate = item(record)
				if self.allow_unuploaded:
					to_delete.append(candidate)
					planned_freed += candidate["size"]
					if planned_freed >= bytes_over:
						break
				else:
					at_risk.append(candidate)
					risk_bytes += candidate["size"]
					if risk_bytes >= needed:
						break

		projected = max(total_bytes - planned_freed, 0)
		shortfall = max(projected - limit_bytes, 0)
		measurement_complete = not unreadable
		return {
			"protected": [str(record["path"]) for record in protected_uploading],
			"protected_uploading": [item(record) for record in protected_uploading],
			"protected_by_min_age": [item(record) for record in protected_by_age],
			"protected_hardlinks": [item(record) for record in protected_hardlinks],
			"unverified_completed": [item(record) for record in unverified_completed],
			"to_delete": to_delete,
			"bytes_freed": planned_freed,
			"total_bytes": total_bytes,
			"projected_total_bytes": projected,
			"limit_bytes": limit_bytes,
			"capacity_shortfall_bytes": shortfall,
			"capacity_target_met": shortfall == 0 and measurement_complete,
			"measurement_complete": measurement_complete,
			"unreadable": unreadable,
			"allow_unuploaded": self.allow_unuploaded,
			"never_uploaded_at_risk": at_risk,
			"never_uploaded_at_risk_count": len(at_risk),
			"never_uploaded_at_risk_bytes": sum(value["size"] for value in at_risk),
		}


def _stat_optional_at(parent_fd: int, name: str) -> Optional[os.stat_result]:
	try:
		return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
	except FileNotFoundError:
		return None


def delete_retention_item(root: Path, item: Dict) -> int:
	"""Delete a planned pair only if both identities still match."""
	data_path = Path(item["path"])
	manifest_path = Path(item["manifest_path"])
	if manifest_path != manifest_path_for(data_path) or manifest_path.parent != data_path.parent:
		raise ValueError("retention manifest path does not match data path")
	parent_fd = _open_parent_beneath(root, data_path)
	lock_fd = -1
	try:
		lock_flags = (
			os.O_RDWR
			| os.O_CREAT
			| getattr(os, "O_CLOEXEC", 0)
			| getattr(os, "O_NOFOLLOW", 0)
		)
		lock_fd = os.open(
			manifest_path.name + ".lock", lock_flags, 0o600, dir_fd=parent_fd
		)
		fcntl.flock(lock_fd, fcntl.LOCK_EX)
		data_stat = _stat_optional_at(parent_fd, data_path.name)
		if data_stat is None or not stat.S_ISREG(data_stat.st_mode):
			raise FileNotFoundError(
				f"planned data file is missing or no longer regular: {data_path}"
			)
		expected_data = item.get("data_identity")
		if not isinstance(expected_data, dict) or not _identity_matches(
			expected_data, data_stat
		):
			raise OSError(
				errno.ESTALE, "data file changed after retention planning", str(data_path)
			)
		manifest_stat = _stat_optional_at(parent_fd, manifest_path.name)
		expected_manifest = item.get("manifest_identity")
		if expected_manifest is None:
			if manifest_stat is not None:
				raise OSError(
					errno.ESTALE,
					"manifest appeared after retention planning",
					str(manifest_path),
				)
		elif (
			manifest_stat is None
			or not stat.S_ISREG(manifest_stat.st_mode)
			or not isinstance(expected_manifest, dict)
			or not _identity_matches(expected_manifest, manifest_stat)
		):
			raise OSError(
				errno.ESTALE,
				"manifest changed after retention planning",
				str(manifest_path),
			)
		os.unlink(data_path.name, dir_fd=parent_fd)
		if manifest_stat is not None:
			os.unlink(manifest_path.name, dir_fd=parent_fd)
		os.fsync(parent_fd)
		return int(data_stat.st_size)
	finally:
		if lock_fd >= 0:
			try:
				fcntl.flock(lock_fd, fcntl.LOCK_UN)
			finally:
				os.close(lock_fd)
		os.close(parent_fd)
