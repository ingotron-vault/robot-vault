"""The one canonical EpisodeStatus vocabulary, and the schema
that has to agree with it.

Mirrors `test_upload_status.py`: the whole point of the shared
enum is that nothing can drift away from it again, so these tests assert the
agreement between the enum and the `episodes` table in
`db/migrations/001_baseline.sql`, rather than just the enum's own values.
"""
import re
from pathlib import Path

from robot_vault_config.episode_status import (
	PERSISTED_EPISODE_STATUSES,
	EpisodeStatus,
	is_terminal,
	parse,
	values,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
BASELINE = REPO_ROOT / "db" / "migrations" / "001_baseline.sql"


def test_persisted_statuses_are_the_five_lifecycle_values_in_order():
	assert [s.value for s in PERSISTED_EPISODE_STATUSES] == [
		"pending",
		"segmented",
		"qa_scored",
		"exported",
		"excluded",
	]


def test_status_is_a_plain_string_on_the_wire_and_on_disk():
	# EpisodeStatus subclasses str so it can be dropped straight into a SQL
	# parameter or a JSON API response without an explicit `.value`.
	assert EpisodeStatus.EXPORTED == "exported"
	assert f"{EpisodeStatus.EXPORTED}" == "exported"


def test_parse_canonicalizes_case_and_whitespace():
	assert parse("segmented") is EpisodeStatus.SEGMENTED
	assert parse("SEGMENTED") is EpisodeStatus.SEGMENTED
	assert parse("  Qa_Scored  ") is EpisodeStatus.QA_SCORED
	assert parse(EpisodeStatus.EXCLUDED) is EpisodeStatus.EXCLUDED


def test_parse_returns_default_instead_of_raising_on_garbage():
	assert parse("banana") is None
	assert parse(None) is None
	assert parse("") is None
	assert parse("banana", EpisodeStatus.PENDING) is EpisodeStatus.PENDING


def test_is_terminal_partitions_the_lifecycle():
	assert is_terminal("exported") and is_terminal("excluded")
	assert not is_terminal("pending")
	assert not is_terminal("segmented")
	assert not is_terminal("qa_scored")
	assert not is_terminal(None)


def test_values_renders_plain_strings_for_sql_in_clauses():
	assert values() == (
		"pending",
		"segmented",
		"qa_scored",
		"exported",
		"excluded",
	)
	assert values((EpisodeStatus.EXPORTED, EpisodeStatus.EXCLUDED)) == (
		"exported",
		"excluded",
	)


# --- baseline schema --------------------------------------------------------


def _migration_sql() -> str:
	assert BASELINE.is_file(), f"missing {BASELINE}"
	return BASELINE.read_text()


def _episodes_table_sql() -> str:
	match = re.search(
		r"CREATE TABLE episodes \((.*?)\n\);", _migration_sql(), flags=re.DOTALL
	)
	assert match, "the baseline no longer creates the episodes table"
	return match.group(1)


def test_episodes_status_check_lists_exactly_the_persisted_statuses():
	"""The database's copy of the vocabulary must equal the enum's."""
	matches = re.findall(
		r"CHECK\s*\(status\s+IN\s+\(([^)]*)\)",
		_episodes_table_sql(),
		flags=re.IGNORECASE,
	)
	assert len(matches) == 1, "episodes must have exactly one status value list"
	listed = re.findall(r"'([^']*)'", matches[0])
	expected = [s.value for s in PERSISTED_EPISODE_STATUSES]
	assert listed == expected, (
		f"the baseline lists {listed}, the enum says {expected}"
	)


def test_migration_has_no_unique_index_on_upload_id_alone():
	"""Segmentation produces more than one episode per upload."""
	sql = _migration_sql()
	assert not re.search(
		r"UNIQUE\s+INDEX\s+\S+\s+ON\s+episodes\s*\(\s*upload_id\s*\)",
		sql,
		flags=re.IGNORECASE,
	)


def test_migration_declares_the_episode_topics_unique_constraint():
	sql = _migration_sql()
	assert "UNIQUE (episode_id, topic)" in sql


def test_migration_has_helpful_indexes():
	sql = _migration_sql()
	assert "idx_episodes_robot_status ON episodes(robot_id, status)" in sql
	assert "idx_episodes_upload_id ON episodes(upload_id)" in sql
	assert "idx_episodes_t_start_ns ON episodes(t_start_ns)" in sql
