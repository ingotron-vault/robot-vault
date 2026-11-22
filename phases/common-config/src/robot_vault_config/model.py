import json
import logging
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# --- gRPC message-size budget ------------------------------------------------
# Both ends cap a single gRPC message at 50 MiB: the client sets
# grpc.max_send/receive_message_length in
# phases/02-robot-upload-agent/src/robot_upload_agent/transport.py, and the
# server sets max_decoding/encoding_message_size in
# phases/03-ingest-server-rs/src/main.rs. `UploadSmall` puts an ENTIRE file in
# one such message, so a file may only take the small path if it fits inside
# that cap with room for the protobuf field/framing overhead that rides along
# with it.
#
# `small_file_threshold_mb` has one default and one hard ceiling, asserted at
# config load. A threshold above the cap would route 50-100 MB files onto a
# path that physically cannot carry them (RESOURCE_EXHAUSTED at send time).
MAX_GRPC_MESSAGE_BYTES: int = 50 * 1024 * 1024
#: Hard ceiling for `upload_agent.small_file_threshold_mb`. 48 rather than 50
#: to leave ~2 MiB of headroom for UploadSmallRequest's metadata/framing.
MAX_SMALL_FILE_THRESHOLD_MB: int = 48
#: The single default, shared by every layer that has to pick one.
DEFAULT_SMALL_FILE_THRESHOLD_MB: int = 4


# --- streaming chunk size ----------------------------------------------------
# A streaming chunk is one gRPC message AND one S3 multipart part, so it is
# squeezed from both ends:
#
#   floor  5 MiB - the ingest server rejects any non-final chunk below the S3
#                  multipart minimum part size (`main.rs`
#                  `chunk_too_small_for_s3_part`). A smaller value does not go
#                  slowly, it fails outright.
#   cap    8 MiB - the ingest server's streaming-only ceiling. The larger
#                  50 MiB gRPC allowance remains available to `UploadSmall`,
#                  but accepted streaming parts are deliberately tighter to
#                  keep the server's concurrent S3 pipeline memory-bounded.
#
# The default is 5 MiB, which is both the S3 floor and -- measured -- the
# fastest option against the current server.
#
# A server that awaits each `upload_part` inline favours 16 MiB (+38% over
# 5 MiB): with 101 serial round trips, fewer and larger parts win. The current
# server uploads parts concurrently, so part count stops being the constraint
# and larger chunks only add per-message latency and buffering:
#
#   500.99 MiB MCAP, single stream, 5 trials each (median, range):
#      5 MiB   2.585 s   193.6 MiB/s  (193.2-193.8)
#      8 MiB   3.013 s   166.1 MiB/s  (161.6-168.0)
#     16 MiB   3.332 s   150.2 MiB/s  (142.5-154.2)
#
# Loopback docker bridge, no TLS, page-cache reads, single-drive object
# store. NOT a robot-link figure -- on a real link the
# bottleneck is the link, not part count.
#
# Consequence: max file size is MAX_PARTS (10000) x chunk size, so 5 MiB caps a
# single upload at ~50 GB and the 8 MiB ceiling permits roughly 80 GB.
MIN_S3_PART_BYTES: int = 5 * 1024 * 1024
MAX_CHUNK_SIZE_BYTES: int = 8 * 1024 * 1024
DEFAULT_CHUNK_SIZE_BYTES: int = 5 * 1024 * 1024


def validate_chunk_size_bytes(value: int, source: str = "config") -> int:
	"""Return a streaming chunk size, or raise ValueError when it is outside
	[5 MiB, 8 MiB].

	Below 5 MiB the ingest server rejects every non-final chunk
	(`chunk_too_small_for_s3_part`). Above 8 MiB it rejects streaming chunks.
	"""
	if isinstance(value, bool) or not isinstance(value, int):
		raise ValueError(f"{source}: upload_agent.chunk_size_bytes must be an integer")
	if not MIN_S3_PART_BYTES <= value <= MAX_CHUNK_SIZE_BYTES:
		raise ValueError(
			f"{source}: upload_agent.chunk_size_bytes must be between "
			f"{MIN_S3_PART_BYTES} (5 MiB, the S3 multipart minimum part size) and "
			f"{MAX_CHUNK_SIZE_BYTES} (8 MiB, the ingest server's streaming limit), got {value}"
		)
	return value


# --- file-level upload concurrency -------------------------------------------
# `parallel_streams` is the number of WHOLE FILES the agent uploads at once,
# each on its own gRPC channel. Never parts of one file: the server validates
# `chunk_number` strictly in order per upload, so splitting one object
# across streams would be rejected.
#
# The ceiling is 4 because the binding constraint is the server's per-robot
# semaphore (`PER_ROBOT_CONCURRENCY`, 2 by default in configs/ingest.env), not
# HTTP/2's stream limit. Asking for more than the server admits does not fail,
# it blocks in `acquire_per_robot` while holding a channel and burning the call
# deadline. The agent probes the real limit over HealthCheck and only falls back
# to the documented default when the server does not advertise one.
MAX_PARALLEL_STREAMS: int = 4
DEFAULT_PARALLEL_STREAMS: int = 2


def validate_small_file_threshold_mb(value: int, source: str = "config") -> int:
	"""Return a small-file threshold, or raise ValueError when one gRPC
	message cannot carry it.

	UploadSmall sends the whole file as one gRPC message, and both ends cap a
	message at 50 MiB. A threshold above MAX_SMALL_FILE_THRESHOLD_MB would send
	files into a path that rejects them.
	"""
	if isinstance(value, bool) or not isinstance(value, int):
		raise ValueError(f"{source}: upload_agent.small_file_threshold_mb must be an integer")
	if not 0 <= value <= MAX_SMALL_FILE_THRESHOLD_MB:
		raise ValueError(
			f"{source}: upload_agent.small_file_threshold_mb must be between 0 and "
			f"{MAX_SMALL_FILE_THRESHOLD_MB}, got {value}"
		)
	return value


@dataclass
class RetentionSettings:
	min_retain_days: int = 7
	max_total_gib: float = 200.0
	# The robot agent's daily retention timer runs `rsm retention
	# plan` and LOGS THE PLAN WITHOUT DELETING unless this is explicitly true.
	# Auto-deleting a customer's recordings on day one is not a default, so the
	# switch is opt-in and lives in vault.json rather than in a flag on a timer.
	auto_apply: bool = False


@dataclass
class PathsSettings:
	robot_root: str = "/robot_data"
	extension: str = ".mcap"


@dataclass
class UploadAgentSettings:
	concurrency: int = 2
	# Queue-local client-side pacing allowance in binary Mibit/s (1 unit is
	# 131,072 payload bytes/s). It covers that queue's workers only; separate
	# processes/queues are not coordinated. 0 means unlimited.
	#
	# The transport consumes the paced stream it is given, so this value takes
	# effect. A default of 50 would cap every robot at 50 Mbit/s = 6.25 MiB/s
	# against a measured 193 MiB/s single-stream baseline (a 31x throughput
	# loss). So the default is an explicit "no cap", and operators with a
	# genuinely metered link set a real number.
	bandwidth_mbps: int = 0
	# 5 MiB: the S3 floor, and measured fastest. See the module header
	# for the floor/cap and the measurement.
	chunk_size_bytes: int = DEFAULT_CHUNK_SIZE_BYTES
	max_retries: int = 3
	base_backoff_seconds: float = 1.0
	# Whole files uploaded at once, each on its own gRPC channel. See the
	# module header: this is a request, further bounded by the server's
	# advertised per-robot limit and MAX_PARALLEL_STREAMS.
	parallel_streams: int = DEFAULT_PARALLEL_STREAMS
	# One default, shared with UploadAgentConfig (phase 02) and capped at
	# MAX_SMALL_FILE_THRESHOLD_MB -- see the module header.
	small_file_threshold_mb: int = DEFAULT_SMALL_FILE_THRESHOLD_MB
	compute_chunk_hashes: bool = False
	# Files whose mtime is younger than this are treated as still-recording and
	# are not upload candidates. Mirrors robot_storage_manager's
	# DEFAULT_STABILITY_SECONDS (kept as a literal so common-config stays
	# dependency-free). 0 turns off the mtime check; the MCAP footer check
	# in is_file_stable always runs.
	min_stable_age_seconds: float = 5.0

	def __post_init__(self) -> None:
		# Refuse a threshold or chunk size that the server cannot accept. This
		# runs for every construction path, including load_config().
		self.small_file_threshold_mb = validate_small_file_threshold_mb(
			self.small_file_threshold_mb, "vault config upload_agent"
		)
		self.chunk_size_bytes = validate_chunk_size_bytes(
			self.chunk_size_bytes, "vault config upload_agent"
		)
		self.parallel_streams = max(1, min(int(self.parallel_streams), MAX_PARALLEL_STREAMS))


@dataclass
class ServerSettings:
	host: str = "localhost"
	port: int = 50051


@dataclass
class TlsSettings:
	"""TLS for the robot upload client's gRPC channel.

	``enabled`` is the only TLS switch. It defaults to true. With no
	``ca_path``, the client uses the platform trust store.
	"""
	enabled: bool = True
	mtls: bool = False
	ca_path: Optional[str] = None
	cert_path: Optional[str] = None
	key_path: Optional[str] = None

	def __post_init__(self) -> None:
		# Treat whitespace-only paths as absent. Without normalising them here a
		# one-way TLS config could pass validation and later try to open a file
		# literally named "   ". Reject non-string path values close to the
		# configuration boundary instead of leaking a cryptic pathlib error.
		for field_name in ("ca_path", "cert_path", "key_path"):
			value = getattr(self, field_name)
			if value is None:
				continue
			if not isinstance(value, str):
				raise ValueError(f"tls.{field_name} must be a filesystem path string")
			setattr(self, field_name, value.strip() or None)


@dataclass
class CaptureSettings:
	"""Capture-metadata intake: operator-declared context for a
	recording session, set once per deployment/session in `vault.json`'s
	`capture` section rather than as a per-run CLI flag (proto/upload.proto
	`UploadMetadata` fields 11-15 carry these end-to-end to the `uploads`
	table). All fields optional and default to None -- a config without a
	`capture` section, one that declares `"capture": {}`, and one that omits
	some of the five keys must behave identically: every downstream layer
	(manifest, proto, ingest SQL writers, query API) already treats "None" and "field absent" the same way, so this dataclass
	does too rather than inventing a second "unset" spelling.

	`session_tags` is a single comma-separated string, not a list: the wire
	representation (`UploadMetadata.session_tags`) is one proto3 `string`
	field and the `uploads` table stores one TEXT column -- free-text
	operator input gets no new repeated-value plumbing.
	"""
	operator: Optional[str] = None
	site: Optional[str] = None
	permission_ref: Optional[str] = None
	session_tags: Optional[str] = None
	task_label: Optional[str] = None


@dataclass
class AuthSettings:
	"""Credential presented to the ingest server.

	``token_file`` is the path to a file that holds this robot's own
	``ignr1.`` credential. The token is not in this config and not in the
	unit's ``Environment=``, because both are readable through
	``systemctl show`` and ``/proc/<pid>/environ``. The file is read on every
	process start, so a rotation needs no config edit.

	A config cannot hold an inline token. When ``token_file`` is not set, the
	upload agent uses the ``INGEST_AUTH_TOKEN`` environment variable (the
	in-compose development topology). When neither is set, it sends no
	credential.
	"""
	token_file: Optional[str] = None


@dataclass
class VaultConfig:
	# This robot's identity in the fleet registry. Declared here,
	# not inferred from the data directory's path, so the same robot keeps its
	# identity no matter where its bags are mounted. A config file must set it
	# (load_config refuses one without it). It must match a row in the
	# `robots` table: ingest refuses uploads from robot ids it does not know.
	# None only for a VaultConfig built in code without a file.
	robot_id: Optional[str] = None
	paths: PathsSettings = field(default_factory=PathsSettings)
	retention: RetentionSettings = field(default_factory=RetentionSettings)
	upload_agent: UploadAgentSettings = field(default_factory=UploadAgentSettings)
	server: ServerSettings = field(default_factory=ServerSettings)
	tls: TlsSettings = field(default_factory=TlsSettings)
	auth: Optional[AuthSettings] = None
	# Capture-metadata intake. None (not a CaptureSettings()
	# default_factory) so every consumer can tell "operator declared a
	# capture section" apart from "config omits this feature".
	capture: Optional[CaptureSettings] = None

	def to_dict(self) -> Dict:
		"""Serialize the configuration. It holds no secrets: the credential
		lives in the file that ``auth.token_file`` names."""
		return asdict(self)

	def validate_tls(self) -> None:
		"""Validate the upload client's TLS settings as one atomic policy.

		``tls.enabled`` is the only switch. mTLS requires the complete
		CA/cert/key set, and no certificate path is accepted with TLS off.
		"""
		tls = self.tls
		if not isinstance(tls, TlsSettings):
			raise ValueError("tls must be a JSON object")
		if type(tls.enabled) is not bool:
			raise ValueError("tls.enabled must be true or false")
		if type(tls.mtls) is not bool:
			raise ValueError("tls.mtls must be true or false")

		has_ca = tls.ca_path is not None
		has_cert = tls.cert_path is not None
		has_key = tls.key_path is not None
		if has_cert != has_key:
			raise ValueError(
				"tls.cert_path and tls.key_path must be configured together"
			)

		if tls.mtls and not tls.enabled:
			raise ValueError("tls.mtls=true requires tls.enabled=true")
		if not tls.enabled:
			if has_ca or has_cert or has_key:
				raise ValueError(
					"TLS certificate paths cannot be configured when TLS is disabled"
				)
			return

		if tls.mtls:
			missing = [
				name
				for name, present in (
					("tls.ca_path", has_ca),
					("tls.cert_path", has_cert),
					("tls.key_path", has_key),
				)
				if not present
			]
			if missing:
				raise ValueError(
					"tls.mtls=true requires tls.ca_path, tls.cert_path, and tls.key_path; "
					f"missing: {', '.join(missing)}"
				)
		elif has_cert or has_key:
			raise ValueError(
				"tls.cert_path and tls.key_path require tls.mtls=true"
			)


def _strict_object(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError(f"duplicate configuration key: {key}")
		result[key] = value
	return result


def _section(data: Dict, name: str, cls):
	value = data[name]
	if not isinstance(value, dict):
		raise ValueError(f"{name} must be a JSON object")
	allowed = {item.name for item in fields(cls)}
	unknown = sorted(set(value) - allowed)
	if unknown:
		raise ValueError(f"unknown {name} configuration key(s): {', '.join(unknown)}")
	return value


def _bounded_int(value, *, name: str, minimum: int, maximum: int) -> int:
	if isinstance(value, bool) or not isinstance(value, int):
		raise ValueError(f"{name} must be an integer")
	if not minimum <= value <= maximum:
		raise ValueError(f"{name} must be between {minimum} and {maximum}")
	return value


def _bounded_number(value, *, name: str, minimum: float, maximum: float) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise ValueError(f"{name} must be a number")
	number = float(value)
	if not minimum <= number <= maximum:
		raise ValueError(f"{name} must be between {minimum} and {maximum}")
	return number


def _validate_config(cfg: VaultConfig, raw: Dict) -> None:
	if not cfg.robot_id:
		raise ValueError("robot_id is required: set it to this robot's registered id")
	if len(cfg.robot_id) > 50:
		raise ValueError("robot_id must contain at most 50 characters")
	if (
		not isinstance(cfg.paths.robot_root, str)
		or not cfg.paths.robot_root
		or not Path(cfg.paths.robot_root).is_absolute()
	):
		raise ValueError("paths.robot_root must be an absolute path")
	if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in cfg.paths.robot_root):
		raise ValueError("paths.robot_root may not contain ASCII whitespace or control characters")
	if (
		not isinstance(cfg.paths.extension, str)
		or not cfg.paths.extension.startswith(".")
		or "/" in cfg.paths.extension
		or "\\" in cfg.paths.extension
	):
		raise ValueError("paths.extension must be a filename extension such as .mcap")

	_bounded_int(cfg.retention.min_retain_days, name="retention.min_retain_days", minimum=0, maximum=36500)
	_bounded_number(cfg.retention.max_total_gib, name="retention.max_total_gib", minimum=0.001, maximum=1_000_000)
	_bounded_int(cfg.upload_agent.concurrency, name="upload_agent.concurrency", minimum=1, maximum=32)
	_bounded_int(cfg.upload_agent.bandwidth_mbps, name="upload_agent.bandwidth_mbps", minimum=0, maximum=1_000_000)
	_bounded_int(cfg.upload_agent.max_retries, name="upload_agent.max_retries", minimum=0, maximum=100)
	_bounded_number(cfg.upload_agent.base_backoff_seconds, name="upload_agent.base_backoff_seconds", minimum=0, maximum=3600)
	_bounded_number(cfg.upload_agent.min_stable_age_seconds, name="upload_agent.min_stable_age_seconds", minimum=0, maximum=86400)
	if "upload_agent" in raw and "parallel_streams" in raw["upload_agent"]:
		_bounded_int(
			raw["upload_agent"]["parallel_streams"],
			name="upload_agent.parallel_streams",
			minimum=1,
			maximum=MAX_PARALLEL_STREAMS,
		)
	if type(cfg.upload_agent.compute_chunk_hashes) is not bool:
		raise ValueError("upload_agent.compute_chunk_hashes must be true or false")
	if not isinstance(cfg.server.host, str) or not cfg.server.host.strip():
		raise ValueError("server.host must be a non-empty string")
	_bounded_int(cfg.server.port, name="server.port", minimum=1, maximum=65535)
	if cfg.auth is not None and cfg.auth.token_file is not None and (
		not isinstance(cfg.auth.token_file, str) or not cfg.auth.token_file.strip()
	):
		raise ValueError("auth.token_file must be a non-empty path when configured")
	for section_name in ("capture",):
		section = getattr(cfg, section_name)
		if section is not None:
			for item in fields(section):
				value = getattr(section, item.name)
				if value is not None and not isinstance(value, str):
					raise ValueError(f"{section_name}.{item.name} must be a string")
	cfg.validate_tls()


def load_config(path: Optional[Path]) -> VaultConfig:
	if path is None:
		cfg = VaultConfig()
		cfg.validate_tls()
		return cfg
	text = Path(path).read_text()
	try:
		data = json.loads(text, object_pairs_hook=_strict_object)
	except Exception:
		raise ValueError("configuration must be strict JSON with unique keys")
	if not isinstance(data, dict):
		raise ValueError("configuration root must be a JSON object")
	allowed_top = {item.name for item in fields(VaultConfig)}
	unknown = sorted(set(data) - allowed_top)
	if unknown:
		raise ValueError(f"unknown top-level configuration key(s): {', '.join(unknown)}")
	cfg = VaultConfig()
	robot_id = data.get("robot_id")
	if robot_id is not None:
		if not isinstance(robot_id, str):
			raise ValueError("robot_id must be a string")
		cfg.robot_id = robot_id.strip() or None
	if "paths" in data:
		cfg.paths = PathsSettings(**_section(data, "paths", PathsSettings))
	if "retention" in data:
		cfg.retention = RetentionSettings(**_section(data, "retention", RetentionSettings))
	if "upload_agent" in data:
		cfg.upload_agent = UploadAgentSettings(**_section(data, "upload_agent", UploadAgentSettings))
	if "server" in data:
		cfg.server = ServerSettings(**_section(data, "server", ServerSettings))
	if "tls" in data:
		cfg.tls = TlsSettings(**_section(data, "tls", TlsSettings))
	# ``auth`` is optional because rua also accepts INGEST_AUTH_TOKEN. An
	# explicit JSON null means the same as omission.
	if data.get("auth") is not None:
		cfg.auth = AuthSettings(**_section(data, "auth", AuthSettings))
	if "capture" in data:
		cfg.capture = CaptureSettings(**_section(data, "capture", CaptureSettings))
	_validate_config(cfg, data)
	return cfg
