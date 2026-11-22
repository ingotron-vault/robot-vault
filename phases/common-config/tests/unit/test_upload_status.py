"""The one canonical UploadStatus vocabulary, and the three places
that have to agree with it.

The whole point of the shared enum is that nothing can drift away from it
again, so these tests assert the agreement rather than just the enum's own
values: the SQL CHECK constraint in the baseline schema, the documented list in
`proto/upload.proto`, and the callers' success check.
"""
import re
from pathlib import Path

import pytest

from robot_vault_config.upload_status import (
	PERSISTED_UPLOAD_STATUSES,
	UploadStatus,
	is_retryable,
	is_success,
	is_terminal,
	parse,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
BASELINE = REPO_ROOT / "db" / "migrations" / "001_baseline.sql"
UPLOAD_PROTO = REPO_ROOT / "proto" / "upload.proto"
INGEST_MAIN_RS = REPO_ROOT / "phases" / "03-ingest-server-rs" / "src" / "main.rs"


def test_persisted_statuses_are_the_four_lifecycle_values():
	assert [s.value for s in PERSISTED_UPLOAD_STATUSES] == [
		"pending",
		"uploading",
		"completed",
		"failed",
	]
	# `unknown` is a wire-only sentinel for "no such upload row" and must not
	# be storable -- see the uploads table in the baseline schema.
	assert UploadStatus.UNKNOWN not in PERSISTED_UPLOAD_STATUSES


def test_status_is_a_plain_string_on_the_wire_and_on_disk():
	# UploadStatus subclasses str so it can be dropped straight into a JSON
	# manifest, a SQL parameter or a protobuf string field.
	assert UploadStatus.COMPLETED == "completed"
	assert f"{UploadStatus.COMPLETED}" == "completed"


@pytest.mark.parametrize(
	"raw,expected",
	[
		("completed", UploadStatus.COMPLETED),
		("COMPLETED", UploadStatus.COMPLETED),
		("  Completed  ", UploadStatus.COMPLETED),
		("uploading", UploadStatus.UPLOADING),
		("unknown", UploadStatus.UNKNOWN),
		(UploadStatus.FAILED, UploadStatus.FAILED),
	],
)
def test_parse_canonicalizes_every_spelling_we_have_written(raw, expected):
	assert parse(raw) is expected


def test_parse_returns_default_instead_of_raising_on_garbage():
	# Callers here read on-disk manifests and wire responses; a surprise value
	# must not crash the agent.
	assert parse("banana") is None
	assert parse(None) is None
	assert parse("") is None
	assert parse("banana", UploadStatus.PENDING) is UploadStatus.PENDING


def test_is_success_matches_only_the_value_the_server_actually_returns():
	"""The bug this replaces: the agent tested `status == "uploaded"`.

	The ingest server writes and returns `"completed"` and has never returned
	`"uploaded"`, so that arm of the check was dead code sitting next to the
	live one.
	"""
	assert is_success("completed") is True
	assert is_success(UploadStatus.COMPLETED) is True
	for other in ("uploading", "failed", "pending", "unknown", "", None):
		assert is_success(other) is False


def test_is_terminal_and_is_retryable_partition_the_lifecycle():
	assert is_terminal("completed") and is_terminal("failed")
	assert not is_terminal("uploading") and not is_terminal("pending")
	# A missing status means "never attempted", which IS a retry candidate --
	# this is what the agent's scan loops rely on.
	assert is_retryable(None) and is_retryable("pending") and is_retryable("failed")
	assert not is_retryable("uploading") and not is_retryable("completed")


def _uploads_table_sql() -> str:
	sql = BASELINE.read_text()
	match = re.search(r"CREATE TABLE uploads \((.*?)\n\);", sql, flags=re.DOTALL)
	assert match, "the baseline no longer creates the uploads table"
	return match.group(1)


def test_uploads_status_check_lists_exactly_the_persisted_statuses():
	"""The database's copy of the vocabulary must equal the enum's."""
	matches = re.findall(
		r"CHECK\s*\(status\s+IN\s+\(([^)]*)\)",
		_uploads_table_sql(),
		flags=re.IGNORECASE,
	)
	assert len(matches) == 1, "uploads must have exactly one status value list"
	listed = re.findall(r"'([^']*)'", matches[0])
	expected = [s.value for s in PERSISTED_UPLOAD_STATUSES]
	assert listed == expected, (
		f"the baseline lists {listed}, the enum says {expected}"
	)


def test_proto_documents_the_same_vocabulary():
	"""proto/upload.proto is the third mirror; its header must list them all."""
	proto = UPLOAD_PROTO.read_text()
	header = proto.split("service UploadService", 1)[0]
	for status in list(PERSISTED_UPLOAD_STATUSES) + [UploadStatus.UNKNOWN]:
		assert f'"{status.value}"' in header, (
			f"proto/upload.proto header does not document {status.value!r}"
		)
	# The field stays a string on purpose (wire compatibility) -- if someone
	# turns it into a proto enum they must revisit this whole module.
	assert "string status = 2;" in proto


def test_ingest_server_only_writes_canonical_statuses():
	"""The Rust server's SQL literals are the fourth copy; keep them in set.

	Scans `main.rs` for `status='...'` / `status <> '...'` / `, 'x', NOW()`
	style literals that feed the `uploads.status` column and asserts each one
	is a canonical value, so a new literal cannot slip past the baseline schema's
	`uploads_status_check` and start failing inserts at runtime.
	"""
	rust = INGEST_MAIN_RS.read_text()
	literals = set(re.findall(r"status\s*(?:=|<>)\s*'([^']*)'", rust))
	assert literals, "no status literals found in main.rs -- did the file move?"
	canonical = {s.value for s in PERSISTED_UPLOAD_STATUSES}
	assert literals <= canonical, f"non-canonical status literal(s): {literals - canonical}"

