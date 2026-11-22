import hashlib
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, List, Sequence, Tuple

logger = logging.getLogger(__name__)

try:  # Optional dependency for unit tests
    import grpc  # type: ignore
except Exception:  # pragma: no cover
    grpc = None

try:
	import upload_pb2 as pb
	import upload_pb2_grpc as pb_grpc
except Exception:  # pragma: no cover
	pb = None
	pb_grpc = None


AUTH_TOKEN_ENV = "INGEST_AUTH_TOKEN"
UPLOAD_RPC_TIMEOUT_ENV = "UPLOAD_RPC_TIMEOUT_SECONDS"
RESUME_RPC_TIMEOUT_ENV = "RESUME_RPC_TIMEOUT_SECONDS"
DEFAULT_UPLOAD_RPC_TIMEOUT_SECONDS = 3600.0
DEFAULT_RESUME_RPC_TIMEOUT_SECONDS = 10.0
MAX_RPC_TIMEOUT_SECONDS = 24 * 60 * 60.0


def _rpc_timeout_seconds(
	explicit: Optional[float],
	*,
	env_name: str,
	default: float,
) -> float:
	"""Resolve a finite RPC deadline and fail before opening a channel."""
	raw = explicit if explicit is not None else os.environ.get(env_name, default)
	try:
		value = float(raw)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{env_name} must be a finite positive number of seconds") from exc
	if not math.isfinite(value) or not 0 < value <= MAX_RPC_TIMEOUT_SECONDS:
		raise ValueError(
			f"{env_name} must be finite, positive, and no greater than "
			f"{MAX_RPC_TIMEOUT_SECONDS:g} seconds"
		)
	return value


def _validate_tls_material(
	use_tls: bool,
	root_certificates: Optional[bytes],
	client_cert_chain: Optional[bytes],
	client_private_key: Optional[bytes],
) -> None:
	"""Reject TLS combinations that gRPC would otherwise silently ignore.

	A client certificate and private key identify one robot and are therefore
	atomic. This client also requires an explicit CA whenever that identity is
	present, matching the shared config's mTLS contract rather than implicitly
	falling back to the host trust store.
	"""
	if type(use_tls) is not bool:
		raise ValueError("use_tls must be true or false")

	material = {
		"root_certificates": root_certificates,
		"client_cert_chain": client_cert_chain,
		"client_private_key": client_private_key,
	}
	supplied = {name for name, value in material.items() if value is not None}
	if not use_tls and supplied:
		raise ValueError(
			"TLS certificate material cannot be supplied when use_tls is false"
		)

	for name, value in material.items():
		if value is not None and not value:
			raise ValueError(f"{name} must not be empty")

	has_cert = client_cert_chain is not None
	has_key = client_private_key is not None
	if has_cert != has_key:
		raise ValueError(
			"client_cert_chain and client_private_key must be supplied together"
		)
	if has_cert and root_certificates is None:
		raise ValueError(
			"mTLS requires root_certificates, client_cert_chain, and client_private_key"
		)


def _read_token_file(path: str) -> Optional[str]:
	"""Read a per-robot credential file, or None if it is absent or unusable.

	Absent is not fatal here: a robot whose token file has not been written yet
	falls through to the next tier, and the ingest server's UNAUTHENTICATED is a
	better diagnostic than a traceback out of a scheduled timer run. Anything
	that IS present but unreadable is reported, because a 0600 file owned by
	another user is an operator mistake that must not look like "no credential
	configured".
	"""
	try:
		raw = Path(path).read_text(encoding="ascii")
	except FileNotFoundError:
		logger.warning("auth.token_file %s does not exist; falling back", path)
		return None
	except (OSError, UnicodeDecodeError) as exc:
		logger.warning("auth.token_file %s is not readable (%s); falling back", path, exc)
		return None
	token = raw.strip()
	return token or None


def resolve_auth_token(
	explicit: Optional[str] = None,
	token_file: Optional[str] = None,
) -> Optional[str]:
	"""Return the ingest credential to present, or None for "no auth".

	Precedence:

	    auth.token_file  ->  explicit  ->  INGEST_AUTH_TOKEN

	`explicit` is a token passed in code (tests and tools that build a
	transport directly). A vault config cannot hold an inline token.

	`token_file` is the enrolled-robot (token profile) form: a per-robot `ignr1.` credential stored
	0600 at a path named by the config, never inlined and never passed through
	`Environment=` (both are readable via `systemctl show` and
	`/proc/<pid>/environ`). It is re-read on every process start, so a rotation
	needs no config edit.

	Only the SOURCE NAME is ever logged, never the value.
	"""
	candidates = (
		("auth.token_file", _read_token_file(token_file) if token_file else None),
		("explicit token", explicit),
		(AUTH_TOKEN_ENV, os.environ.get(AUTH_TOKEN_ENV)),
	)
	for source, candidate in candidates:
		if candidate and candidate.strip():
			logger.info("ingest credential resolved from %s", source)
			return candidate.strip()
	return None


class GrpcUploadTransport:
	def __init__(
		self,
		host: str,
		port: int,
		use_tls: bool = True,
		root_certificates: Optional[bytes] = None,
		client_cert_chain: Optional[bytes] = None,
		client_private_key: Optional[bytes] = None,
		auth_token: Optional[str] = None,
		robot_id: Optional[str] = None,
		auth_token_file: Optional[str] = None,
		upload_rpc_timeout_seconds: Optional[float] = None,
		resume_rpc_timeout_seconds: Optional[float] = None,
	):
		self.address = f"{host}:{port}"
		self.upload_rpc_timeout_seconds = _rpc_timeout_seconds(
			upload_rpc_timeout_seconds,
			env_name=UPLOAD_RPC_TIMEOUT_ENV,
			default=DEFAULT_UPLOAD_RPC_TIMEOUT_SECONDS,
		)
		self.resume_rpc_timeout_seconds = _rpc_timeout_seconds(
			resume_rpc_timeout_seconds,
			env_name=RESUME_RPC_TIMEOUT_ENV,
			default=DEFAULT_RESUME_RPC_TIMEOUT_SECONDS,
		)
		# This robot's declared identity, from `vault.json`'s top-level
		# `robot_id`. `_metadata_from_manifest` refuses to send without it.
		self.robot_id = (robot_id or "").strip() or None
		_validate_tls_material(
			use_tls,
			root_certificates,
			client_cert_chain,
			client_private_key,
		)
		if grpc is None:
			raise ImportError("grpc is not installed; install grpcio to use GrpcUploadTransport")
		opts = [("grpc.max_send_message_length", 50*1024*1024), ("grpc.max_receive_message_length", 50*1024*1024)]
		if use_tls:
			if client_cert_chain is not None:
				creds = grpc.ssl_channel_credentials(
					root_certificates=root_certificates,
					private_key=client_private_key,
					certificate_chain=client_cert_chain,
				)
			else:
				creds = grpc.ssl_channel_credentials(root_certificates=root_certificates)
			self.channel = grpc.secure_channel(self.address, creds, options=opts)
		else:
			self.channel = grpc.insecure_channel(self.address, options=opts)
		# The ingest server rejects unauthenticated RPCs with
		# UNAUTHENTICATED when INGEST_AUTH_TOKEN is configured server-side, so
		# every call below carries this metadata. Empty when no token is
		# configured, which keeps a token-less server (and the fake servers in
		# the unit tests) working unchanged.
		# auth.token_file wins over the inlined/legacy tiers, and the
		# file is read HERE -- on every transport construction -- so a rotated
		# token is picked up without a config edit or a restart of anything but
		# the one-shot run itself.
		self.auth_metadata: Sequence[Tuple[str, str]] = _auth_metadata(
			resolve_auth_token(auth_token, token_file=auth_token_file)
		)
		self.stub = pb_grpc.UploadServiceStub(self.channel) if pb_grpc else None

	def close(self) -> None:
		"""Release the channel.

		Needed because file-level concurrency builds one INDEPENDENT channel
		per worker, and a pool that never closed them would leak a TCP
		connection and a gRPC completion queue per worker per drain.
		"""
		channel = getattr(self, "channel", None)
		if channel is not None:
			try:
				channel.close()
			except Exception:  # pragma: no cover - closing twice is harmless
				pass

	def health_check(self) -> Dict[str, Any]:
		"""HealthCheck's status plus its `checks` map, or {} if the RPC fails.

		Used to read the server's advertised per-robot stream limit (see
		`server_per_robot_stream_limit`). Never raises: an agent that cannot ask
		must still be able to upload.
		"""
		if not (pb and self.stub) or not hasattr(self.stub, "HealthCheck"):
			return {}
		try:
			resp = self.stub.HealthCheck(pb.HealthCheckRequest(), metadata=self.auth_metadata, timeout=5.0)
		except Exception:
			return {}
		return {
			"status": getattr(resp, "status", ""),
			"checks": dict(getattr(resp, "checks", {}) or {}),
		}

	def server_per_robot_stream_limit(self) -> Optional[int]:
		"""How many concurrent uploads this server will admit from one robot.

		Read from HealthCheck's `checks` map so the client is not guessing.
		Exceeding the server's per-robot semaphore does not fail -- it blocks in
		`acquire_per_robot` -- but a blocked stream still holds a channel and
		burns the call deadline, so it is worth asking.

		Returns None when the server advertises nothing, leaving the caller to
		fall back to the documented default (see
		`robot_vault_config.model.MAX_PARALLEL_STREAMS`'s note).
		"""
		checks = self.health_check().get("checks") or {}
		for key in PER_ROBOT_LIMIT_CHECK_KEYS:
			raw = checks.get(key)
			if raw is None:
				continue
			try:
				value = int(str(raw).strip())
			except (TypeError, ValueError):
				continue
			if value > 0:
				return value
		return None

	def upload_small(self, manifest: Dict[str, Any], data: bytes) -> Dict[str, object]:
		assert pb and self.stub, "Proto stubs not available; generate python proto code first"
		md = _metadata_from_manifest(manifest, self.robot_id)
		req = pb.UploadSmallRequest(metadata=md, data=data)
		resp = self.stub.UploadSmall(
			req,
			metadata=self.auth_metadata,
			timeout=self.upload_rpc_timeout_seconds,
		)
		return _response_to_dict(resp)

	def upload_stream(
			self,
			manifest: Dict[str, Any],
			chunks: Optional[Iterable[bytes]] = None,
		) -> Dict[str, object]:
		"""Negotiate a durable-part plan, then send only that plan's missing bytes.

		The server must answer resume version 1. Its session token binds the
		missing-part snapshot to the server multipart upload; ABORTED causes the
		caller's retry loop to re-probe before opening another stream.
		"""
		assert pb and self.stub, "Proto stubs not available; generate python proto code first"
		file_path = Path(manifest.get("path", ""))
		file_stat = file_path.stat()
		file_size = file_stat.st_size
		identity = _file_identity(file_stat)
		if manifest.get("file_size_bytes") != file_size:
			raise ValueError("recording size changed after hashing; prepare the recording again")
		chunk_size = resolve_chunk_size(manifest)
		plan = compute_chunks_plan(file_size, chunk_size)
		if not manifest.get("upload_id"):
			raise ValueError("recording has no upload_id; prepare the recording again")
		# Probe failures (connection, authentication, deadline) reach the retry
		# policy instead of silently discarding durable progress.
		r = self.stub.ResumeUpload(
			pb.ResumeUploadRequest(upload_id=manifest["upload_id"], resume_version=1),
			metadata=self.auth_metadata,
			timeout=self.resume_rpc_timeout_seconds,
		)
		if getattr(r, "resume_version", 0) != 1:
			raise ValueError("server did not negotiate resume version 1")
		if getattr(r, "upload_id", "") != manifest.get("upload_id"):
			raise ValueError("resume response upload_id does not match this recording")
		confirmed_bytes = int(getattr(r, "bytes_received", 0))
		session_id = str(getattr(r, "resume_session_id", ""))
		advertised_chunk_size = int(getattr(r, "chunk_size_bytes", 0))
		if getattr(r, "status", "") == "completed":
			if confirmed_bytes != file_size or list(r.missing_chunks) or session_id or advertised_chunk_size:
				raise ValueError("completed resume response has an inconsistent file size")
			# Upload validates the immutable metadata and replays its receipt.
			plan = []
		elif session_id:
			if advertised_chunk_size != chunk_size:
				raise ValueError("resume chunk size differs from this recording's pinned chunk size")
			plan = compute_chunks_plan(
				file_size, chunk_size, confirmed_bytes, list(r.missing_chunks), partial_resume=True,
			)
		elif confirmed_bytes or advertised_chunk_size or list(getattr(r, "missing_chunks", [])):
			raise ValueError("resume progress requires a durable session token")
		# A plan-aware caller persists the server-confirmed count and reads only
		# missing ranges. Plain iterable callers are cut to the same plan.
		notify_plan = getattr(chunks, "on_resume", None)
		if callable(notify_plan):
			notify_plan(confirmed_bytes, True)
		want_hashes = bool(manifest.get("compute_chunk_hashes"))

		def gen():
			if _file_identity(file_path.stat()) != identity:
				raise ValueError("recording changed during resume negotiation")
			md = _metadata_from_manifest(manifest, self.robot_id)
			md.resume_version = 1
			md.chunk_size_bytes = chunk_size
			md.resume_session_id = session_id
			yield pb.UploadRequest(metadata=md)
			read_plan = getattr(chunks, "for_plan", None)
			if callable(read_plan):
				numbered = _number_plan(read_plan(plan), plan)
			elif chunks is None:
				numbered = _number_plan(_read_plan(file_path, plan, identity), plan)
			else:
				numbered = _select_plan(chunks, plan, chunk_size)
			for number, offset, buf, is_final in numbered:
				chunk_hash = hashlib.sha256(buf).hexdigest() if want_hashes else ""
				yield pb.UploadRequest(chunk=pb.UploadChunk(
					data=buf, offset=offset, chunk_number=number,
					is_final=is_final, chunk_hash=chunk_hash,
				))

		resp = self.stub.Upload(gen(), metadata=self.auth_metadata, timeout=self.upload_rpc_timeout_seconds)
		if _file_identity(file_path.stat()) != identity:
			raise ValueError("recording changed during upload")
		return _response_to_dict(resp)


#: HealthCheck `checks` keys the agent will accept as the per-robot stream limit,
#: in preference order. More than one because the server side of this
#: advertisement is owned by phase 03; whichever name it publishes, the client
#: reads it rather than hard-coding the value.
PER_ROBOT_LIMIT_CHECK_KEYS: Tuple[str, ...] = (
	"per_robot_concurrency",
	"per_robot_limit",
	"max_concurrent_streams_per_robot",
)


def _file_identity(value: os.stat_result) -> Tuple[int, int, int, int, int]:
	return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read_plan(file_path: Path, plan: List[Tuple[int, int, int]], identity: Optional[Tuple[int, int, int, int, int]] = None) -> Iterator[bytes]:
	"""Read only the planned ranges for standalone transport callers."""
	with file_path.open("rb") as f:
		for _idx, offset, length in plan:
			if identity is not None and _file_identity(os.fstat(f.fileno())) != identity:
				raise ValueError("recording changed while reading upload parts")
			f.seek(offset)
			yield f.read(length)


def number_chunks(chunks: Iterable[bytes], chunk_size: int) -> Iterator[Tuple[int, int, bytes, bool]]:
	"""Turn an iterable of buffers into `(chunk_number, offset, data, is_final)`.

	One-buffer lookahead, because `is_final` is only knowable once the next read
	comes back empty -- and the server needs it on the last chunk to complete the
	multipart upload.

	Enforces the pinned-chunk-size invariant: every non-final
	buffer must be exactly `chunk_size`. A caller that drifts would shift every
	subsequent S3 part boundary, and a drifted buffer under 5 MiB is rejected
	outright by the server (`chunk_too_small_for_s3_part`) after the bytes have
	already been sent. Cheaper to catch here.
	"""
	offset = 0
	number = 0
	pending: Optional[Tuple[int, int, bytes]] = None
	for buf in chunks:
		if not buf:
			continue
		if pending is not None:
			if len(pending[2]) != chunk_size:
				raise ValueError(
					f"chunk_size drift: chunk {pending[0]} is {len(pending[2])} bytes, "
					f"expected the pinned {chunk_size}"
				)
			yield pending[0], pending[1], pending[2], False
		pending = (number, offset, buf)
		offset += len(buf)
		number += 1
	if pending is not None:
		yield pending[0], pending[1], pending[2], True


def _auth_metadata(token: Optional[str]) -> Sequence[Tuple[str, str]]:
	"""gRPC call metadata carrying the shared token, or an empty tuple.

	Sent as `authorization: Bearer <token>` — the same header the ingest
	server's admin router checks, so one token covers both transports.
	"""
	if not token:
		return ()
	return (("authorization", f"Bearer {token}"),)


#: Fallback when a manifest carries no (or an unusable) `chunk_size_bytes`.
#: Imported from common-config rather than redeclared: a 1 MiB literal would be
#: *below* the server's 5 MiB S3 part floor, so any upload that fell back to it
#: would fail every non-final chunk with `chunk_too_small_for_s3_part`. One
#: definition, one floor.
from robot_vault_config.model import DEFAULT_CHUNK_SIZE_BYTES  # noqa: E402


def resolve_chunk_size(manifest: Dict[str, Any]) -> int:
	"""Return the chunk size to use, pinned to the persisted manifest value.

	Part boundaries must never change between upload attempts. A retry that
	re-chunked with a different size would produce different S3 part offsets and
	corrupt the reassembled object. So the chunk size is always taken from the
	`chunk_size_bytes` persisted at upload start; if it is missing or invalid we
	fall back to a fixed default (still deterministic across retries).
	"""
	persisted = manifest.get("chunk_size_bytes")
	try:
		size = int(persisted) if persisted else 0
	except (TypeError, ValueError):
		size = 0
	if size <= 0:
		size = DEFAULT_CHUNK_SIZE_BYTES
	return size


def compute_chunks_plan(file_size: int, chunk_size: int, resume_bytes: int = 0, missing_chunks: Optional[List[int]] = None, *, partial_resume: bool = False) -> List[Tuple[int, int, int]]:
	"""Build original chunk indices/ranges; narrow only after v1 negotiation."""
	if file_size < 0 or chunk_size <= 0:
		raise ValueError("invalid recording or chunk size")
	total = (file_size + chunk_size - 1) // chunk_size
	if total > 10000:
		raise ValueError("recording requires more than 10000 upload parts; increase the chunk size or split the recording")
	indices = list(range(total))
	if partial_resume:
		if missing_chunks is None or any(type(index) is not int or index < 0 or index >= total for index in missing_chunks):
			raise ValueError("resume response contains invalid missing chunk numbers")
		if missing_chunks != sorted(set(missing_chunks)):
			raise ValueError("resume missing chunks must be unique and ordered")
		indices = missing_chunks
	plan = [(index, index * chunk_size, min(chunk_size, file_size - index * chunk_size)) for index in indices]
	if partial_resume and resume_bytes != file_size - sum(length for _, _, length in plan):
		raise ValueError("resume durable byte count does not match its missing chunks")
	return plan


def _number_plan(chunks: Iterable[bytes], plan: List[Tuple[int, int, int]]) -> Iterator[Tuple[int, int, bytes, bool]]:
	"""Keep original numbering and mark the last transmitted part as final."""
	from itertools import zip_longest
	for position, (entry, data) in enumerate(zip_longest(plan, chunks)):
		if entry is None or data is None or len(data) != entry[2]:
			raise ValueError("recording bytes do not match the negotiated upload plan")
		number, offset, _length = entry
		yield number, offset, data, position == len(plan) - 1


def _select_plan(chunks: Iterable[bytes], plan: List[Tuple[int, int, int]], chunk_size: int) -> Iterator[Tuple[int, int, bytes, bool]]:
	if not plan:
		return
	needed = {number: (offset, length) for number, offset, length in plan}
	seen = set()
	for number, offset, data, _final in number_chunks(chunks, chunk_size):
		if number not in needed:
			continue
		if needed[number] != (offset, len(data)):
			raise ValueError("recording bytes do not match the negotiated upload plan")
		seen.add(number)
		yield number, offset, data, number == plan[-1][0]
	if seen != set(needed):
		raise ValueError("recording ended before all missing upload parts were read")


def _metadata_from_manifest(
	manifest: Dict[str, Any], robot_id: Optional[str] = None
):
	# Lazy import to avoid hard dep in tests without generated stubs
	import upload_pb2 as pb
	md = pb.UploadMetadata(
		robot_id=require_robot_id(robot_id),
		filename=Path(manifest.get("path", "")).name,
		file_size=manifest.get("file_size_bytes", 0),
		content_type="application/octet-stream",
		labels={},
		upload_id=manifest.get("upload_id") or "",
		expected_sha256=manifest.get("sha256_hash") or "",
		# Capture metadata comes straight from the manifest
		# (robot_storage_manager.core.load_or_create_manifest populates these
		# from cfg.capture at manifest-creation time). `or ""` because proto3
		# has no distinct "unset" for a string field -- "" IS the wire
		# default, and the ingest server maps it back to SQL NULL, so a
		# manifest whose capture keys are None round-trips to NULL exactly
		# like an upload without capture metadata does.
		operator=manifest.get("operator") or "",
		site=manifest.get("site") or "",
		permission_ref=manifest.get("permission_ref") or "",
		session_tags=manifest.get("session_tags") or "",
		task_label=manifest.get("task_label") or "",
	)
	return md


def require_robot_id(configured: Optional[str]) -> str:
	"""This robot's identity, from `vault.json`'s top-level `robot_id`.

	The identity is a declared fact, never inferred from where a file sits on
	disk. Raise ValueError when it is missing, so no upload goes out under a
	guessed robot.
	"""
	robot_id = (configured or "").strip()
	if not robot_id:
		raise ValueError("robot_id is not configured; set robot_id in the vault config")
	return robot_id


def _response_to_dict(resp: Any) -> Dict[str, object]:
	return {
		"upload_id": getattr(resp, "upload_id", ""),
		"status": getattr(resp, "status", ""),
		"bytes_received": getattr(resp, "bytes_received", 0),
		"final_hash": getattr(resp, "final_hash", ""),
		"storage_path": getattr(resp, "storage_path", ""),
	}
