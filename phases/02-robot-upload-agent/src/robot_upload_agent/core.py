import logging
import os
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import time as _time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from prometheus_client import Counter, Histogram, Gauge

from robot_vault_config.model import (
	DEFAULT_CHUNK_SIZE_BYTES,
	DEFAULT_PARALLEL_STREAMS,
	DEFAULT_SMALL_FILE_THRESHOLD_MB,
	MAX_PARALLEL_STREAMS,
	MAX_CHUNK_SIZE_BYTES,
	VaultConfig,
	load_config,
	validate_chunk_size_bytes,
	validate_small_file_threshold_mb,
)
from robot_vault_config.upload_status import (
	UploadStatus,
	is_retryable,
	is_success,
	parse as parse_status,
)
from robot_storage_manager.core import (
	DEFAULT_STABILITY_SECONDS,
	_stat_identity,
	_same_manifest_content_strict,
	_same_manifest_content,
	_is_valid_sha256,
	manifest_path_for,
	read_manifest,
	compute_sha256_for_file,
	ConcurrentManifestUpdateError,
	is_file_stable,
	load_or_create_manifest,
	save_manifest,
	scan_for_data_files,
)
from . import retry as _retry
from .bandwidth import BandwidthLimiter, UploadCancelled
from .transport import GrpcUploadTransport, compute_chunks_plan
from .smart_batching import ConnectionMeasurements, SmartBatcher

logger = logging.getLogger(__name__)

def recording_evidence(path: Path, manifest: Dict) -> Dict:
	"""Read historical upload proof without contacting the server or changing files."""
	try:
		identity = _stat_identity(path.stat()) if path.is_file() else None
	except (OSError, ValueError):
		identity = None
	receipt = manifest.get("upload_receipt")
	saved_bytes = manifest.get("saved_bytes", 0)
	if (
		type(saved_bytes) is not int or saved_bytes < 0 or identity is None
		or saved_bytes > identity["file_size_bytes"] or not _same_manifest_content_strict(manifest, identity)
	):
		saved_bytes = 0
	verified = (
		isinstance(receipt, dict)
		and identity is not None
		and parse_status(manifest.get("status")) is UploadStatus.COMPLETED
		and _same_manifest_content_strict(receipt, identity)
		and _same_manifest_content_strict(manifest, identity)
		and _is_valid_sha256(manifest.get("sha256_hash"))
		and receipt.get("sha256_hash") == manifest.get("sha256_hash")
		and bool(manifest.get("upload_id"))
		and receipt.get("upload_id") == manifest.get("upload_id")
		and isinstance(receipt.get("storage_path"), str)
		and bool(receipt["storage_path"])
		and isinstance(receipt.get("verified_at"), str)
		and bool(receipt["verified_at"])
	)
	return {
		"data_file_present": identity is not None,
		"server_verified": True if verified else None,
		"server_verified_at": receipt["verified_at"] if verified else None,
		"server_storage_path": receipt["storage_path"] if verified else None,
		# A receipt is historical; deletion still needs the retention checks.
		"safe_to_remove_local": False,
		"last_upload_error": manifest.get("last_upload_error") if isinstance(manifest.get("last_upload_error"), dict) else None,
		"upload_updated_at": manifest.get("upload_updated_at"),
		"saved_bytes": saved_bytes,
		"resume_supported": manifest.get("resume_supported"),
	}


#: Fallback for the server's per-robot concurrent-upload limit, used only when
#: the server advertises nothing over HealthCheck. Mirrors
#: `PER_ROBOT_CONCURRENCY` in `configs/ingest.env`. A documented constant rather
#: than a magic 2 buried in the concurrency math -- and never a client-side
#: config knob, because the value belongs to the server.
ASSUMED_SERVER_PER_ROBOT_STREAMS: int = 2


@dataclass
class UploadAgentConfig:
	# This robot's declared identity, carried on every upload's metadata
	# and in the upload_id seed. The transport refuses to upload without it
	# (transport.require_robot_id).
	robot_id: Optional[str] = None
	server_host: str = "localhost"
	server_port: int = 50051
	tls: bool = True
	concurrency: int = 2
	# 0 == unlimited, and that is the default. See
	# robot_vault_config.UploadAgentSettings.bandwidth_mbps for why: the pacing
	# this value controls only works because the transport consumes the
	# generator it is handed, and a default of 50 would cap every robot at
	# 6.25 MiB/s against a measured 193 MiB/s single-stream baseline.
	bandwidth_mbps: int = 0
	# 5 MiB: the S3 floor, and measured fastest against the pipelined server
	# (see robot_vault_config.model); floor/cap enforced by
	# validate_chunk_size_bytes (5 MiB S3 part floor, 8 MiB server stream cap).
	chunk_size_bytes: int = DEFAULT_CHUNK_SIZE_BYTES
	max_retries: int = 3
	base_backoff_seconds: float = 1.0
	# Whole files in flight at once, each on its own gRPC channel. The
	# effective count is min(this, the connection strategy's value, the server's
	# advertised per-robot limit, MAX_PARALLEL_STREAMS).
	parallel_streams: int = DEFAULT_PARALLEL_STREAMS
	# ONE default, shared with robot_vault_config.UploadAgentSettings.
	# Anything above MAX_SMALL_FILE_THRESHOLD_MB (48) cannot be sent at all --
	# UploadSmall puts the whole file in a single gRPC message and both ends
	# cap a message at 50 MiB -- so __post_init__ refuses it outright.
	small_file_threshold_mb: int = DEFAULT_SMALL_FILE_THRESHOLD_MB
	compute_chunk_hashes: bool = False
	# A file whose mtime is younger than this is treated as still being
	# recorded and is not an upload candidate (see is_file_stable and
	# UploadQueue._is_stable). The default deliberately matches phase-01's
	# DEFAULT_STABILITY_SECONDS rather than 0: a 0 default would make the gate
	# inert and leave the exact hole it closes (uploading a half-written,
	# still-growing MCAP), while 5s only ever delays a file by one scan cycle.
	# Set to 0 to turn off the mtime part. The MCAP footer check always runs.
	min_stable_age_seconds: float = DEFAULT_STABILITY_SECONDS

	def __post_init__(self) -> None:
		self.small_file_threshold_mb = validate_small_file_threshold_mb(
			self.small_file_threshold_mb, "UploadAgentConfig"
		)
		self.chunk_size_bytes = validate_chunk_size_bytes(
			self.chunk_size_bytes, "UploadAgentConfig"
		)
		self.parallel_streams = max(1, min(int(self.parallel_streams), MAX_PARALLEL_STREAMS))

	@staticmethod
	def from_vault(cfg: VaultConfig) -> "UploadAgentConfig":
		# ``VaultConfig`` can be assembled directly by callers as well as loaded
		# from JSON. Validate here too so those callers cannot bypass the
		# tls.enabled/mTLS invariants enforced by load_config().
		cfg.validate_tls()
		return UploadAgentConfig(
			robot_id=cfg.robot_id,
			server_host=cfg.server.host,
			server_port=cfg.server.port,
			tls=cfg.tls.enabled,
			concurrency=cfg.upload_agent.concurrency,
			bandwidth_mbps=cfg.upload_agent.bandwidth_mbps,
			chunk_size_bytes=cfg.upload_agent.chunk_size_bytes,
			max_retries=cfg.upload_agent.max_retries,
			base_backoff_seconds=cfg.upload_agent.base_backoff_seconds,
			parallel_streams=cfg.upload_agent.parallel_streams,
			small_file_threshold_mb=cfg.upload_agent.small_file_threshold_mb,
			compute_chunk_hashes=cfg.upload_agent.compute_chunk_hashes,
			min_stable_age_seconds=float(cfg.upload_agent.min_stable_age_seconds),
		)


	def to_dict(self) -> Dict:
		return asdict(self)


def get_network_type() -> Dict[str, any]:
	"""Detect network connection type (WiFi, Cellular, Ethernet, etc.)"""
	try:
		import psutil
		network_info = {
			"type": "unknown",
			"interface": None,
			"ssid": None,
			"is_wifi": False,
			"is_cellular": False,
			"is_ethernet": False
		}
		
		# Get network interfaces
		interfaces = psutil.net_if_addrs()
		stats = psutil.net_if_stats()
		
		# Find active interface with internet connection
		for interface_name, addresses in interfaces.items():
			if interface_name in stats and stats[interface_name].isup:
				# Skip loopback
				if interface_name.startswith('lo'):
					continue
					
				# Check if interface has an IP address
				has_ip = any(addr.family == 2 for addr in addresses)  # AF_INET = 2
				if not has_ip:
					continue
				
				# Determine interface type
				interface_lower = interface_name.lower()
				
				if any(x in interface_lower for x in ['wlan', 'wifi', 'wlp']):
					network_info.update({
						"type": "wifi",
						"interface": interface_name,
						"is_wifi": True
					})
					# Try to get WiFi SSID
					try:
						ssid_out = subprocess.check_output(["iwgetid", "-r"], stderr=subprocess.DEVNULL, text=True, timeout=1.0)
						network_info["ssid"] = ssid_out.strip() or None
					except Exception:
						pass
					break
				elif any(x in interface_lower for x in ['wwan', 'ppp', 'usb', 'tty', 'modem']):
					network_info.update({
						"type": "cellular",
						"interface": interface_name,
						"is_cellular": True
					})
					break
				elif any(x in interface_lower for x in ['eth', 'enp', 'ens']):
					network_info.update({
						"type": "ethernet",
						"interface": interface_name,
						"is_ethernet": True
					})
					# Don't break - prefer WiFi/cellular over ethernet
		
		return network_info
		
	except Exception as e:
		return {
			"type": "unknown",
			"interface": None,
			"ssid": None,
			"is_wifi": False,
			"is_cellular": False,
			"is_ethernet": False,
			"error": str(e)
		}



def check_server_connectivity(host: str, port: int, timeout: float = 3.0) -> bool:
	"""Check if the server is reachable by attempting a connection"""
	import socket
	try:
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(timeout)
		result = sock.connect_ex((host, port))
		sock.close()
		return result == 0
	except Exception:
		return False


def ensure_sha256_hash(path: Path, manifest: Dict) -> Dict:
	"""Ensure the manifest carries a real sha256_hash before it is used for upload.

	transport.py sends expected_sha256=manifest.get("sha256_hash") or "" to the
	server, and the server only verifies integrity when that value is non-empty.
	Without this, uploads that never went through the manual `rsm hash` CLI ship
	with zero end-to-end integrity checking. Compute and persist the hash here
	(streaming, via compute_sha256_for_file) whenever it's missing/empty.
	"""
	if not manifest.get("sha256_hash"):
		manifest["sha256_hash"] = compute_sha256_for_file(path)
		save_manifest(manifest)
	return manifest


def compute_upload_id_for_manifest(manifest: Dict, robot_id: Optional[str]) -> str:
	"""Deterministic upload_id for one recording on one robot.

	The seed includes ``robot_id``. Without it, byte-identical files at the
	same path and mtime on two robots get the same id, and the server refuses
	the second robot's upload with a permanent ALREADY_EXISTS.
	"""
	path = manifest.get("path", "")
	size = str(manifest.get("file_size_bytes", 0))
	sha = manifest.get("sha256_hash") or ""
	try:
		mtime = str(Path(path).stat().st_mtime_ns)
	except Exception:
		mtime = "0"
	seed = "|".join([(robot_id or "").strip(), path, size, sha, mtime])
	return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _pin_upload_chunk_size(manifest: Dict, configured: int) -> None:
	if not manifest.get("upload_id"):
		manifest.pop("saved_bytes", None)
		manifest.pop("resume_supported", None)
	if not manifest.get("upload_id") or not manifest.get("chunk_size_bytes"):
		minimum = (manifest["file_size_bytes"] + 9999) // 10000
		chunk_size = max(configured, minimum)
		if chunk_size > MAX_CHUNK_SIZE_BYTES:
			raise ValueError("recording exceeds the maximum 10000 upload parts at the supported chunk size; split the recording")
		manifest["chunk_size_bytes"] = chunk_size


class _FileChunkSource:
	"""Read and pace only negotiated ranges while keeping sent bytes separate."""
	def __init__(self, path: Path, manifest: Dict, chunk_size: int, limiter: BandwidthLimiter, on_progress=None):
		self.path = path
		self.manifest = manifest
		self.chunk_size = chunk_size
		self.limiter = limiter
		self.on_progress = on_progress
		self.sent_bytes = 0

	def __iter__(self):
		return self.for_plan(compute_chunks_plan(self.manifest["file_size_bytes"], self.chunk_size))

	def on_resume(self, saved_bytes: int, supported: bool) -> None:
		self.manifest["saved_bytes"] = saved_bytes
		self.manifest["resume_supported"] = supported
		save_manifest(self.manifest)

	def for_plan(self, plan):
		self.sent_bytes = 0
		total = sum(length for _, _, length in plan)
		if callable(self.on_progress):
			self.on_progress(str(self.path), 0, total)
		with self.path.open("rb") as file_obj:
			for _number, offset, length in plan:
				if not _same_manifest_content_strict(self.manifest, _stat_identity(os.fstat(file_obj.fileno()))):
					raise ValueError("recording changed after hashing; retry after recording has stopped")
				file_obj.seek(offset)
				data = file_obj.read(length)
				if len(data) != length or not _same_manifest_content_strict(self.manifest, _stat_identity(os.fstat(file_obj.fileno()))):
					raise ValueError("recording changed while uploading; retry after recording has stopped")
				self.limiter.acquire(len(data))
				self.sent_bytes += len(data)
				if callable(self.on_progress):
					self.on_progress(str(self.path), self.sent_bytes, total)
				yield data


class UploadQueue:
	def __init__(self, root: Path, config: Optional[UploadAgentConfig] = None, vault_config_path: Optional[Path] = None, transport: Optional[GrpcUploadTransport] = None, clock: Optional[Callable[[], float]] = None) -> None:
		self.root = root
		self.vault = load_config(vault_config_path) if vault_config_path else VaultConfig()
		self.config = config or UploadAgentConfig.from_vault(self.vault)
		self.bandwidth_limiter = BandwidthLimiter(int(self.config.bandwidth_mbps))
		self.transport = transport  # Lazy init to avoid grpc dep in tests
		# Injectable wall clock (epoch seconds) for the file-stability gate;
		# lets tests advance "now" instead of sleeping or rewriting mtimes.
		self.clock = clock
		# Probe the path that matters: this ingest server, not a public DNS/IP
		# whose reachability says nothing about the upload route.
		self.smart_batcher = SmartBatcher(
			probe_host=self.config.server_host,
			measurement_provider=self._measure_connection_path,
		)
		# File-level concurrency: each worker thread gets its OWN transport,
		# i.e. its own gRPC channel and therefore its own TCP connection. Sharing
		# one channel would put N HTTP/2 streams on one connection, sharing one
		# congestion window -- worth ~nothing on the link-limited path this is
		# supposed to help.
		self._worker_transports: List[GrpcUploadTransport] = []
		self._worker_local = threading.local()
		self._transport_lock = threading.Lock()
		# Cached HealthCheck answer for the server's per-robot stream limit;
		# probed once per queue rather than per file.
		self._server_stream_limit: Optional[int] = None
		self._server_stream_limit_probed = False
		self._last_effective_parallel_streams = 0

		# Initialize metrics for smart batching effectiveness
		try:
			self.upload_duration_by_quality = Histogram(
				"upload_duration_seconds_by_quality",
				"Upload duration by connection quality",
				labelnames=["connection_quality"],
				buckets=(1, 5, 10, 30, 60, 120, 300, 600)
			)
		except ValueError:
			# Metric already registered
			from prometheus_client import REGISTRY
			self.upload_duration_by_quality = REGISTRY._names_to_collectors.get("upload_duration_seconds_by_quality")
		
		try:
			self.bytes_uploaded_by_quality = Counter(
				"bytes_uploaded_by_quality",
				"Total bytes uploaded by connection quality",
				labelnames=["connection_quality"]
			)
		except ValueError:
			from prometheus_client import REGISTRY
			self.bytes_uploaded_by_quality = REGISTRY._names_to_collectors.get("bytes_uploaded_by_quality")
		
		try:
			self.upload_speed_mbps = Gauge(
				"upload_speed_mbps",
				"Current upload speed in Mbps",
				labelnames=["connection_quality"]
			)
		except ValueError:
			from prometheus_client import REGISTRY
			self.upload_speed_mbps = REGISTRY._names_to_collectors.get("upload_speed_mbps")

	def _measure_connection_path(self) -> ConnectionMeasurements:
		"""Measure ingest, unless an injected test transport supplies no signal."""

		if self.transport is not None and not isinstance(
			self.transport, GrpcUploadTransport
		):
			provider = getattr(self.transport, "connection_measurements", None)
			if callable(provider):
				measured = provider()
				if isinstance(measured, ConnectionMeasurements):
					return measured
			return ConnectionMeasurements()
		return self.smart_batcher.measure_ingest_path()

	def _new_transport(self) -> GrpcUploadTransport:
		"""Build a fresh transport with its own independent gRPC channel."""
		# Defensive re-validation catches a directly constructed or subsequently
		# mutated VaultConfig before any credential files are read. In particular,
		# mTLS must never degrade to one-way TLS because one path was omitted.
		self.vault.validate_tls()
		root_bytes = cert_bytes = key_bytes = None
		if self.vault.tls.enabled:
			if self.vault.tls.ca_path:
				root_bytes = Path(self.vault.tls.ca_path).read_bytes()
			if self.vault.tls.mtls:
				# validate_tls() guarantees that both paths are non-empty here.
				cert_bytes = Path(self.vault.tls.cert_path).read_bytes()
				key_bytes = Path(self.vault.tls.key_path).read_bytes()
		# auth.token_file (this robot's own `ignr1.` credential,
		# stored 0600 and never inlined) first, then INGEST_AUTH_TOKEN -- both
		# resolved inside the transport, with no credential if neither is set.
		auth_cfg = getattr(self.vault, "auth", None)
		return GrpcUploadTransport(
			self.config.server_host,
			self.config.server_port,
			self.config.tls,
			root_certificates=root_bytes,
			client_cert_chain=cert_bytes,
			client_private_key=key_bytes,
			auth_token_file=getattr(auth_cfg, "token_file", None),
			# The declared identity; the transport refuses to send without it.
			robot_id=self.config.robot_id,
		)

	def _worker_transport(self) -> GrpcUploadTransport:
		"""This thread's transport.

		An injected transport (the unit tests' fakes) is reused as-is; otherwise
		each worker thread builds its own, once, and it is closed by
		`_close_worker_transports` when the drain finishes. Independent channels
		are the whole point of the concurrency work -- see the note in
		`__init__` and `smart_batching.BATCH_STRATEGIES`.
		"""
		if self.transport is not None:
			return self.transport
		existing = getattr(self._worker_local, "transport", None)
		if existing is not None:
			return existing
		transport = self._new_transport()
		self._worker_local.transport = transport
		with self._transport_lock:
			self._worker_transports.append(transport)
		return transport

	def _close_worker_transports(self) -> None:
		with self._transport_lock:
			transports, self._worker_transports = self._worker_transports, []
		for t in transports:
			close = getattr(t, "close", None)
			if callable(close):
				close()
		self._worker_local = threading.local()

	def _resolve_parallel_streams(self, work_items: int, allow_probe: bool = False) -> int:
		"""How many whole files to upload at once.

		`min(config.parallel_streams, strategy.parallel_streams, server limit,
		MAX_PARALLEL_STREAMS, work_items)`.

		The server's limit is asked for, not assumed: exceeding
		`PER_ROBOT_CONCURRENCY` does not fail, it blocks in `acquire_per_robot`
		while the extra worker holds an idle channel and burns its deadline, so
		over-requesting is pure cost. `ASSUMED_SERVER_PER_ROBOT_STREAMS` is the
		documented fallback for a server that advertises nothing.
		"""
		quality = self.smart_batcher.detect_connection_quality()
		strategy = self.smart_batcher.BATCH_STRATEGIES[quality]
		want = min(
			int(getattr(self.config, "parallel_streams", DEFAULT_PARALLEL_STREAMS)),
			int(strategy.parallel_streams),
			MAX_PARALLEL_STREAMS,
			max(1, int(work_items)),
		)
		return max(1, min(want, self._server_stream_capacity(allow_probe)))

	def _server_stream_capacity(self, allow_probe: bool = False) -> int:
		"""The server's per-robot concurrent-upload limit.

		`allow_probe` gates the HealthCheck RPC: only the drain path pays for it.
		A read-only caller like `describe()` reports the documented assumption
		rather than opening a channel as a side effect of rendering status.
		"""
		if not self._server_stream_limit_probed:
			probe = getattr(self.transport, "server_per_robot_stream_limit", None) if self.transport else None
			throwaway = None
			if probe is None and allow_probe and self.transport is None:
				# Ask over a throwaway channel rather than guessing silently --
				# and not over one of the worker channels, which must stay
				# independent per worker.
				try:
					throwaway = self._new_transport()
					probe = getattr(throwaway, "server_per_robot_stream_limit", None)
				except Exception:
					probe = None
			if callable(probe):
				self._server_stream_limit_probed = True
				try:
					self._server_stream_limit = probe()
				except Exception:
					self._server_stream_limit = None
			if throwaway is not None:
				throwaway.close()
		limit = self._server_stream_limit
		return int(limit) if limit and limit > 0 else ASSUMED_SERVER_PER_ROBOT_STREAMS

	def _is_stable(self, path: Path) -> bool:
		"""Is `path` finished, and so safe to hash and upload?

		A robot that is still recording keeps extending the file, so hashing
		and streaming it now would ship a partial file whose sha256 no longer
		matches its final content. Gate every upload-candidate selection on
		this. The mtime must be old enough (min_stable_age_seconds <= 0 turns
		that part off), and a .mcap file must end with a complete MCAP footer
		(always checked). See robot_storage_manager.core.is_file_stable.
		"""
		min_age = float(getattr(self.config, "min_stable_age_seconds", DEFAULT_STABILITY_SECONDS))
		now = self.clock() if callable(self.clock) else None
		return is_file_stable(path, min_age_seconds=min_age, now=now)

	def describe(
		self, files: Optional[List[Path]] = None, *, probe_server: bool = False
	) -> Dict:
		# Web callers supply a pre-confined list so symlink targets are rejected
		# before any manifest is read or created. CLI callers retain the normal
		# root scan by omitting it.
		files = scan_for_data_files(self.root) if files is None else files
		pending = []
		for f in files:
			mf = load_or_create_manifest(f, cfg=self.vault)
			if is_retryable(mf.get("status")):
				pending.append(str(f))
		
		# Get network type information
		network_info = get_network_type()
		
		# Check server connectivity
		server_reachable = check_server_connectivity(self.config.server_host, self.config.server_port)
		
		# Always consider connected if server is reachable (no WiFi restrictions)
		conn = server_reachable
		
		# Get connection quality for smart batching
		connection_quality = self.smart_batcher.detect_connection_quality()
		strategy = self.smart_batcher.BATCH_STRATEGIES[connection_quality]
		
		effective_streams = self._resolve_parallel_streams(
			MAX_PARALLEL_STREAMS, allow_probe=probe_server
		)
		server_streams = self._server_stream_capacity(allow_probe=False)

		return {
			"root": str(self.root), 
			"pending_count": len(pending), 
			"connected": conn,
			"server_reachable": server_reachable,
			"server_host": self.config.server_host,
			"server_port": self.config.server_port,
			"network_type": network_info["type"],
			"network_interface": network_info["interface"],
			"network_ssid": network_info.get("ssid"),
			"is_wifi": network_info["is_wifi"],
			"is_cellular": network_info["is_cellular"],
			"is_ethernet": network_info["is_ethernet"],
			"connection_quality": connection_quality.value,
			"connection_measurements": self.smart_batcher.connection_measurements(),
			"batch_strategy": {
				"parallel_streams": strategy.parallel_streams,
				"retry_multiplier": strategy.retry_multiplier,
			},
			# What the agent would ACTUALLY run at, after the config ceiling and
			# the server's advertised per-robot limit. Reported separately from
			# the strategy's request so the two are never confused.
			"configured_parallel_streams": int(self.config.parallel_streams),
			"server_per_robot_streams": server_streams,
			"server_streams_source": (
				"advertised" if self._server_stream_limit else "assumed"
			),
			"effective_parallel_streams": effective_streams,
			"chunk_size_bytes": int(self.config.chunk_size_bytes),
			"bandwidth_mbps": int(self.config.bandwidth_mbps),
			"pending": pending[:10]
		}

	def list_files(self, files: Optional[List[Path]] = None) -> List[Dict]:
		# See describe(): the web boundary supplies canonical candidates.
		files = scan_for_data_files(self.root) if files is None else files
		items: List[Dict] = []
		for f in files:
			mf = read_manifest(manifest_path_for(f)) or {}
			p = Path(f)
			file_stat = p.stat()
			# Match the live-file view used by upload selection without writing
			# manifests during GET. History retains the previous upload status.
			if not _same_manifest_content(mf, _stat_identity(file_stat)):
				mf = {
					**mf,
					"status": UploadStatus.PENDING.value,
					"sha256_hash": None,
					"upload_id": None,
				}
			items.append({
				"path": str(p),
				"name": p.name,
				"size": file_stat.st_size,
				"mtime": file_stat.st_mtime,
				"status": parse_status(mf.get("status"), UploadStatus.PENDING).value,
				**recording_evidence(p, mf),
			})
		return items

	def _upload_file(self, path: Path, manifest: Dict, on_progress: Optional[Callable[[str, int, int], None]] = None) -> Dict:
		identity = _stat_identity(path.stat())
		if not _same_manifest_content_strict(manifest, identity):
			raise ValueError("recording changed after hashing; retry after recording has stopped")
		size = identity["file_size_bytes"]
		threshold = self.config.small_file_threshold_mb * 1024 * 1024
		transport = self._worker_transport()

		# Track upload start time for metrics
		upload_start_time = _time.perf_counter()

		# Get smart batching strategy based on connection quality
		connection_quality = self.smart_batcher.detect_connection_quality()

		# Preserve the size saved before the first attempt, including after
		# a process restart or configuration change.
		chunk_size = int(manifest.get("chunk_size_bytes") or self.config.chunk_size_bytes)
		validate_chunk_size_bytes(chunk_size, "recording's pinned upload chunk size")

		# Per-chunk hashes are computed in the transport, on the bytes it actually
		# sends. Computing them here would not work: the transport never reads a
		# manifest["chunk_hash"], so the server's chunk verification would never run.
		manifest["compute_chunk_hashes"] = bool(getattr(self.config, "compute_chunk_hashes", False))

		if size <= threshold:
			data = path.read_bytes()
			if not _same_manifest_content_strict(manifest, _stat_identity(path.stat())):
				raise ValueError("recording changed while reading; retry after recording has stopped")
			self.bandwidth_limiter.acquire(len(data))
			if callable(on_progress):
				on_progress(str(path), len(data), size)
			result = transport.upload_small(manifest, data)
			sent_size = len(data)
		else:
			manifest["chunk_size_bytes"] = int(chunk_size)
			manifest["connection_quality"] = connection_quality.value
			chunks = _FileChunkSource(path, manifest, chunk_size, self.bandwidth_limiter, on_progress)
			result = transport.upload_stream(manifest, chunks)
			sent_size = chunks.sent_bytes
		if not _same_manifest_content_strict(manifest, _stat_identity(path.stat())):
			raise ValueError("recording changed during upload; retry after recording has stopped")

		# Record metrics for smart batching effectiveness
		upload_duration = _time.perf_counter() - upload_start_time
		quality_label = connection_quality.value

		if self.upload_duration_by_quality:
			self.upload_duration_by_quality.labels(connection_quality=quality_label).observe(upload_duration)

		if self.bytes_uploaded_by_quality and is_success(result.get("status")):
			self.bytes_uploaded_by_quality.labels(connection_quality=quality_label).inc(sent_size)

		if upload_duration > 0 and sent_size > 0:
			speed_mbps = (sent_size * 8) / (upload_duration * 1024 * 1024)
			if self.upload_speed_mbps:
				self.upload_speed_mbps.labels(connection_quality=quality_label).set(speed_mbps)
			if is_success(result.get("status")):
				self.smart_batcher.record_upload_observation(
					bytes_uploaded=sent_size, duration_seconds=upload_duration
				)

		return result

	def _upload_file_with_retry(
		self,
		path: Path,
		manifest: Dict,
		on_progress: Optional[Callable[[str, int, int], None]] = None,
	) -> Dict:
		"""One file, retried on transient gRPC failures with full-jitter backoff.

		Each attempt probes durable server parts again and sends only missing
		parts. Backoff and retry limits
		bound repeated failures without discarding the server's saved parts.
		"""
		strategy = self.smart_batcher.BATCH_STRATEGIES[self.smart_batcher.detect_connection_quality()]
		size = path.stat().st_size if path.exists() else 0
		last_attempt_bytes = 0

		def attempt(n: int) -> Dict:
			nonlocal last_attempt_bytes
			sent = 0
			def progress(file_path: str, bytes_sent: int, total: int) -> None:
				nonlocal sent
				sent = max(sent, bytes_sent)
				if callable(on_progress):
					on_progress(file_path, bytes_sent, total)
			if n:
				logger.warning("retrying upload of %s (attempt %d)", path, n + 1)
			try:
				return self._upload_file(path, manifest, on_progress=progress)
			finally:
				last_attempt_bytes = sent

		def on_retry(n: int, code: str, delay: float) -> None:
			logger.warning(
				"upload of %s failed with %s; retrying in %.2fs (attempt %d of %d)",
				path, code, delay, n + 2, int(self.config.max_retries) + 1,
			)

		result, outcome = _retry.call_with_retry(
			attempt,
			max_retries=int(self.config.max_retries),
			base_backoff_seconds=float(self.config.base_backoff_seconds),
			retry_multiplier=float(strategy.retry_multiplier),
			file_size_bytes=size,
			on_retry=on_retry,
			attempt_bytes=lambda: last_attempt_bytes,
		)
		if outcome.attempts > 1:
			manifest["upload_attempts"] = outcome.attempts
			manifest["retry_codes"] = list(outcome.codes)
		return result

	def _upload_one(
		self,
		path: Path,
		manifest: Dict,
		on_progress: Optional[Callable[[str, int, int], None]] = None,
	) -> Tuple[Optional[str], Optional[Dict]]:
		"""Drive one file through the manifest state machine. Returns (uploaded, error).

		Extracted so `run_once`, `run_many` and `upload_selected` share exactly one
		copy of the status transitions -- and so the concurrent paths can call it
		from a worker thread. Everything it touches is per-file (a manifest
		sidecar, written atomically by `save_manifest`), so it needs no lock.
		"""
		# Mark as uploading *before* streaming so retention (which never deletes
		# 'uploading' files) actually protects this file while it's in flight, and
		# restore on failure so it doesn't get stuck "uploading" forever if the
		# process dies mid-transfer.
		manifest["status"] = UploadStatus.UPLOADING.value
		manifest["upload_updated_at"] = datetime.now(timezone.utc).isoformat()
		manifest.pop("last_upload_error", None)
		manifest.pop("upload_receipt", None)
		save_manifest(manifest)
		try:
			identity = _stat_identity(path.stat())
			resp = self._upload_file_with_retry(path, manifest, on_progress=on_progress)
		except UploadCancelled:
			manifest["status"] = UploadStatus.FAILED.value
			manifest["upload_updated_at"] = datetime.now(timezone.utc).isoformat()
			save_manifest(manifest)
			raise
		except Exception as exc:
			if not isinstance(exc, (OSError, ValueError, ConcurrentManifestUpdateError)) and not _retry.status_code_name(exc):
				manifest["status"] = UploadStatus.FAILED.value
				manifest["upload_updated_at"] = datetime.now(timezone.utc).isoformat()
				save_manifest(manifest)
				raise
			resp = {"status": "exception", "detail": str(exc)}
			code = _retry.status_code_name(exc)
			if code:
				resp["code"] = code
		except BaseException:
			manifest["status"] = UploadStatus.FAILED.value
			manifest["upload_updated_at"] = datetime.now(timezone.utc).isoformat()
			save_manifest(manifest)
			raise
		manifest["upload_updated_at"] = datetime.now(timezone.utc).isoformat()
		if is_success(resp.get("status")):
			manifest["status"] = UploadStatus.COMPLETED.value
			try:
				current_identity = _stat_identity(path.stat())
			except OSError:
				current_identity = {}
			if (
				resp.get("status") == UploadStatus.COMPLETED.value
				and _same_manifest_content_strict(identity, current_identity)
				and _same_manifest_content_strict(manifest, identity)
				and _is_valid_sha256(manifest.get("sha256_hash"))
				and resp.get("final_hash") == manifest.get("sha256_hash")
				and bool(manifest.get("upload_id"))
				and resp.get("upload_id") == manifest.get("upload_id")
				and resp.get("bytes_received") == identity["file_size_bytes"]
				and isinstance(resp.get("storage_path"), str)
				and bool(resp["storage_path"])
			):
				manifest["upload_receipt"] = {
					**identity,
					"upload_id": manifest["upload_id"],
					"sha256_hash": manifest["sha256_hash"],
					"storage_path": resp["storage_path"],
					"verified_at": manifest["upload_updated_at"],
				}
				manifest["saved_bytes"] = identity["file_size_bytes"]
			save_manifest(manifest)
			return str(path), None
		manifest["status"] = UploadStatus.FAILED.value
		manifest["last_upload_error"] = resp
		save_manifest(manifest)
		return None, {"file": str(path), "error": resp}

	def _prepare(self, path: Path) -> Dict:
		"""Hash the file if needed and give it a deterministic upload_id."""
		mf = load_or_create_manifest(path, cfg=self.vault)
		mf = ensure_sha256_hash(path, mf)
		_pin_upload_chunk_size(mf, self.config.chunk_size_bytes)
		mf["upload_id"] = mf.get("upload_id") or compute_upload_id_for_manifest(mf, self.config.robot_id)
		return mf

	@staticmethod
	def _preparation_error(path: Path, exc: Exception) -> Dict:
		"""Expected filesystem/manifest faults belong to one candidate only."""
		status = "missing" if isinstance(exc, FileNotFoundError) else "preparation_failed"
		return {"file": str(path), "error": {"status": status, "detail": str(exc)}}

	def cancel(self) -> None:
		"""Wake workers waiting for bandwidth during queue shutdown."""
		self.bandwidth_limiter.cancel()

	def set_bandwidth_mbps(self, mbps: int) -> None:
		"""Apply a web job's rate before any worker starts."""
		limiter = BandwidthLimiter(mbps)
		self.config.bandwidth_mbps = mbps
		self.bandwidth_limiter = limiter

	def _drain(
		self,
		work: List[Tuple[Path, Dict]],
		on_progress: Optional[Callable[[str, int, int], None]] = None,
	) -> Tuple[List[str], List[Dict]]:
		"""Upload `work` with file-level concurrency, honouring parallel_streams.

		The unit of concurrency is a WHOLE FILE. Parts of one object are never
		interleaved: the server validates the pinned part plan and holds an
		upload lease, so splitting one file across streams would be rejected --
		and each attempt of one file is serialised after the previous attempt's
		terminal state by the retry loop, never concurrent with it, so two streams
		can never race on one `upload_id`'s S3 key or `uploads` row.

		This helps U2 (draining a backlog of N files) and does NOTHING for U1 (one
		big file). Say exactly that in any claim.
		"""
		uploaded: List[str] = []
		errors: List[Dict] = []
		if not work:
			self._last_effective_parallel_streams = 0
			return uploaded, errors
		streams = self._resolve_parallel_streams(len(work), allow_probe=True)
		self._last_effective_parallel_streams = streams
		try:
			if streams <= 1:
				for path, manifest in work:
					ok, err = self._upload_one(path, manifest, on_progress=on_progress)
					if ok:
						uploaded.append(ok)
					elif err:
						errors.append(err)
				return uploaded, errors
			# Threads, not asyncio: gRPC's C-core and hashlib both release the GIL
			# for I/O and large buffers, so the workers really do overlap.
			with ThreadPoolExecutor(max_workers=streams, thread_name_prefix="rua-upload") as pool:
				try:
					results = list(pool.map(
						lambda item: self._upload_one(item[0], item[1], on_progress=on_progress),
						work,
					))
				except BaseException:
					# ThreadPoolExecutor waits for workers on exit. Wake those
					# currently pacing when Ctrl-C or cancellation reaches the caller.
					self.cancel()
					raise
		finally:
			self._close_worker_transports()
		for ok, err in results:
			if ok:
				uploaded.append(ok)
			elif err:
				errors.append(err)
		return uploaded, errors

	def run_once(self) -> Dict:
		# Always allow uploads (no SSID check)
		files = scan_for_data_files(self.root)
		skipped: List[str] = []
		errors: List[Dict] = []
		for f in files:
			try:
				mf = load_or_create_manifest(f, cfg=self.vault)
				if not is_retryable(mf.get("status")):
					continue
				# Still being written? Leave it for a later scan rather than
				# hashing/uploading a partial file.
				if not self._is_stable(f):
					skipped.append(str(f))
					continue
				mf = self._prepare(f)
			except (OSError, ValueError, ConcurrentManifestUpdateError) as exc:
				errors.append(self._preparation_error(f, exc))
				continue
			try:
				ok, err = self._upload_one(f, mf)
			finally:
				self._close_worker_transports()
			if ok:
				return {"uploaded": ok, "skipped": skipped, "errors": errors}
			errors.append(err)
			break
		result = {"uploaded": None, "skipped": skipped, "errors": errors}
		if errors:
			result["error"] = errors[0]["error"]
		return result

	def run_many(
		self,
		max_files: Optional[int] = None,
		files: Optional[List[Path]] = None,
	) -> Dict:
		"""Drain up to `max_files` (default: config.concurrency) pending files.

		The files are uploaded with file-level concurrency, bounded by
		`_resolve_parallel_streams` -- so `max_files` bounds *how many files this
		call takes on*, and `parallel_streams` bounds *how many are in flight at
		once*.

		Returns a summary with uploaded list and errors.
		"""
		# Always allow uploads (no SSID check)
		limit = max_files if isinstance(max_files, int) else int(self.config.concurrency)
		# max_files <= 0 means "take everything pending" -- what `rua upload --all`
		# asks for when draining a docked robot's whole backlog.
		unlimited = limit <= 0
		skipped: List[str] = []
		errors: List[Dict] = []
		work: List[Tuple[Path, Dict]] = []
		# Web callers pass paths that were resolved and confined before this
		# method can create/read manifests. CLI callers omit the argument.
		files = scan_for_data_files(self.root) if files is None else files
		for f in files:
			if not unlimited and len(work) >= limit:
				break
			try:
				mf = load_or_create_manifest(f, cfg=self.vault)
				if not is_retryable(mf.get("status")):
					continue
				# See run_once: a file that's still being written is not a
				# candidate yet, it just waits for the next scan.
				if not self._is_stable(f):
					skipped.append(str(f))
					continue
				work.append((f, self._prepare(f)))
			except (OSError, ValueError, ConcurrentManifestUpdateError) as exc:
				errors.append(self._preparation_error(f, exc))
		uploaded, upload_errors = self._drain(work)
		errors.extend(upload_errors)
		quality = self.smart_batcher.detect_connection_quality()
		return {
			"uploaded": uploaded,
			"errors": errors,
			"skipped": skipped,
			"connection_quality": quality.value,
			"connection_measurements": self.smart_batcher.connection_measurements(),
			"configured_parallel_streams": int(self.config.parallel_streams),
			"server_per_robot_streams": self._server_stream_capacity(False),
			"server_streams_source": (
				"advertised" if self._server_stream_limit else "assumed"
			),
			"effective_parallel_streams": self._last_effective_parallel_streams,
		}

	def upload_selected(self, paths: List[Path], on_progress: Optional[Callable[[str, int, int], None]] = None) -> Dict:
		# Always allow uploads (no SSID check)
		uploaded: List[str] = []
		errors: List[Dict] = []
		
		# Prepare stable files for priority and size ordering
		file_metadata = []
		for p in paths:
			fp = Path(p)
			try:
				fp = fp.resolve()
				if not fp.is_file():
					errors.append({"file": str(fp), "error": {"status": "missing"}})
					continue
				mf = load_or_create_manifest(fp, cfg=self.vault)
				if not is_retryable(mf.get("status")):
					continue
				# An explicit selection is still gated: the operator picked a
				# file, not a moment, and can't see that the robot is mid-write.
				# Uploading now would ship a partial file under a sha256 that
				# stops matching as soon as the recorder appends again. Unlike
				# the scan loops there is no "next scan" to retry silently, so
				# surface it as a per-file error the UI/job store can show.
				if not self._is_stable(fp):
					errors.append({"file": str(fp), "error": {
						"status": "unstable",
						"detail": (
							f"still recording: modified within the last {self.config.min_stable_age_seconds}s, "
							"or the MCAP footer is not written yet"
						),
					}})
					continue
				mf = self._prepare(fp)
				file_metadata.append({
					"path": fp,
					"manifest": mf,
					"size": fp.stat().st_size,
					"priority": mf.get("priority", "normal"),
				})
			except (OSError, ValueError, ConcurrentManifestUpdateError) as exc:
				errors.append(self._preparation_error(fp, exc))
		
		if file_metadata:
			# The worker pool consumes one ordered list of whole files.
			# Dedupe by resolved path AND by upload_id: two concurrent streams
			# carrying the same upload_id would race on one S3 key and one
			# `uploads` row, because the server calls create_multipart_upload
			# unconditionally per stream.
			work: List[Tuple[Path, Dict]] = []
			seen_paths = set()
			seen_upload_ids = set()
			for file_info in self.smart_batcher.order_files(file_metadata):
				fp = file_info["path"]
				mf = file_info["manifest"]
				mf["upload_id"] = mf.get("upload_id") or compute_upload_id_for_manifest(mf, self.config.robot_id)
				if str(fp) in seen_paths or mf["upload_id"] in seen_upload_ids:
					continue
				seen_paths.add(str(fp))
				seen_upload_ids.add(mf["upload_id"])
				work.append((fp, mf))
			ok, errs = self._drain(work, on_progress=on_progress)
			uploaded.extend(ok)
			errors.extend(errs)

		return {"uploaded": uploaded, "errors": errors}
