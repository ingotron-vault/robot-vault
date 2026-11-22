"""Canonical upload-status vocabulary.

Before this module the same lifecycle was spelled four different ways:

  1. robot-local manifests (`*.manifest.json`) wrote
     `pending`/`uploading`/`uploaded`/`failed`;
  2. `proto/upload.proto` documented the wire field as free text
     ("uploading, completed, failed") with nothing enforcing it;
  3. `phases/03-ingest-server-rs/src/main.rs` wrote the SQL literals
     `'uploading'`/`'completed'`/`'failed'` into `uploads.status`;
  4. the upload agent's success check compared the *server's* response
     against the *manifest's* word `"uploaded"` -- a value the server has
     never returned.

There is now exactly one vocabulary, defined here, mirrored by the comments
in `proto/upload.proto` and enforced on the database side by the
`uploads_status_check` CHECK constraint added in
`db/migrations/001_baseline.sql`.

The module is deliberately dependency-free so every phase (and the migration
test) can import it without pulling in grpc/psycopg/pyarrow.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable, Optional, Tuple, Union


class UploadStatus(str, Enum):
	"""The lifecycle of a single upload, from both ends of the wire.

	`str` subclass on purpose: `UploadStatus.COMPLETED == "completed"` is
	True, so the value can be dropped straight into a JSON manifest, a SQL
	parameter or a protobuf string field without an explicit `.value`.
	"""

	#: Discovered on the robot, hashed or not, not yet handed to the server.
	PENDING = "pending"
	#: Bytes are in flight. Written by the agent before it starts streaming
	#: and by the ingest server when it first records the upload row.
	UPLOADING = "uploading"
	#: The server stored (and, when a hash was supplied, verified) the whole
	#: object. This is the ONLY success value.
	COMPLETED = "completed"
	#: The attempt ended badly; the file is a candidate for another try.
	FAILED = "failed"
	#: Wire-only sentinel: the ingest server answers `GetUploadStatus` /
	#: `ResumeUpload` with this when it holds no row for the upload id. It is
	#: never persisted, so it is NOT part of `PERSISTED_UPLOAD_STATUSES` and
	#: the SQL CHECK constraint rejects it.
	UNKNOWN = "unknown"

	def __str__(self) -> str:  # pragma: no cover - trivial
		return self.value


#: The values `uploads.status` (Postgres) and a robot manifest's `status` may
#: hold. Kept as an ordered tuple because the baseline schema
#: (`db/migrations/001_baseline.sql`) lists them in this order and a unit
#: test asserts the two agree.
PERSISTED_UPLOAD_STATUSES: Tuple[UploadStatus, ...] = (
	UploadStatus.PENDING,
	UploadStatus.UPLOADING,
	UploadStatus.COMPLETED,
	UploadStatus.FAILED,
)

def parse(
	value: Union[str, UploadStatus, None],
	default: Optional[UploadStatus] = None,
) -> Optional[UploadStatus]:
	"""Coerce a status string into a canonical value.

	Case-insensitive and whitespace-tolerant. Returns `default` for
	unknown/empty input rather
	than raising -- callers here are readers of on-disk or over-the-wire data
	and must not crash on garbage.
	"""
	if isinstance(value, UploadStatus):
		return value
	if value is None:
		return default
	text = str(value).strip().lower()
	if not text:
		return default
	try:
		return UploadStatus(text)
	except ValueError:
		return default


def is_success(value: Union[str, UploadStatus, None]) -> bool:
	"""True only for the one success value the ingest server actually returns.

	This is the check the upload agent applies to an `UploadResponse.status`;
	it replaces the old `status == "completed" or status == "uploaded"` test,
	whose second arm could never fire.
	"""
	return parse(value) is UploadStatus.COMPLETED


def is_terminal(value: Union[str, UploadStatus, None]) -> bool:
	"""True when no further transition is expected without a new attempt."""
	return parse(value) in (UploadStatus.COMPLETED, UploadStatus.FAILED)


def is_retryable(value: Union[str, UploadStatus, None]) -> bool:
	"""True when the agent should pick this file up on the next scan."""
	return parse(value, UploadStatus.PENDING) in (
		UploadStatus.PENDING,
		UploadStatus.FAILED,
	)


def values(statuses: Iterable[UploadStatus] = PERSISTED_UPLOAD_STATUSES) -> Tuple[str, ...]:
	"""The plain strings for `statuses` -- handy for SQL `IN (...)` rendering."""
	return tuple(s.value for s in statuses)
