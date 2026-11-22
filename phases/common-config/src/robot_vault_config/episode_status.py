"""Canonical episode-status vocabulary.

Mirrors `upload_status.py`: one enum, defined once, that every
phase importing `robot_vault_config` shares, and that the database enforces via
a CHECK constraint added in `db/migrations/001_baseline.sql`
(`episodes_status_check`). A unit test
(`phases/common-config/tests/unit/test_episode_status.py`) parses that
migration and fails if the two ever disagree -- the same discipline that
keeps `UploadStatus` and `uploads_status_check` in sync.

An episode moves through this lifecycle roughly in order, though not every
episode visits every state (a bad episode can jump straight to `excluded`
from any earlier state):

  pending    -- the episode boundary was recorded but no rows are cut yet.
  segmented  -- rows exist for the episode.
  qa_scored  -- the QA engine (Pro) has produced a report for it.
  exported   -- included in at least one dataset cut (Pro).
  excluded   -- deliberately dropped from downstream use (failed QA, an
                operator veto, ...); recorded rather than deleted so the
                decision -- and its reason -- stays auditable.

The module is deliberately dependency-free, exactly like `upload_status.py`,
so every phase (and the migration test) can import it without pulling in
grpc/psycopg/pyarrow.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable, Optional, Tuple, Union


class EpisodeStatus(str, Enum):
	"""The lifecycle of a single segmented episode.

	`str` subclass on purpose: `EpisodeStatus.EXPORTED == "exported"` is True,
	so the value can be dropped straight into a SQL parameter or a JSON API
	response without an explicit `.value`.
	"""

	#: Boundary recorded, no rows cut for it yet.
	PENDING = "pending"
	#: Rows exist for this episode (segmentation stage has run for it).
	SEGMENTED = "segmented"
	#: The QA engine has produced a report for this episode.
	QA_SCORED = "qa_scored"
	#: Included in at least one dataset export/cut.
	EXPORTED = "exported"
	#: Deliberately excluded from downstream use (failed QA, operator veto).
	EXCLUDED = "excluded"

	def __str__(self) -> str:  # pragma: no cover - trivial
		return self.value


#: The values `episodes.status` (Postgres) may hold. Kept as an ordered tuple
#: because `db/migrations/001_baseline.sql` lists them in this order and a
#: unit test asserts the two agree.
PERSISTED_EPISODE_STATUSES: Tuple[EpisodeStatus, ...] = (
	EpisodeStatus.PENDING,
	EpisodeStatus.SEGMENTED,
	EpisodeStatus.QA_SCORED,
	EpisodeStatus.EXPORTED,
	EpisodeStatus.EXCLUDED,
)


def parse(
	value: Union[str, EpisodeStatus, None],
	default: Optional[EpisodeStatus] = None,
) -> Optional[EpisodeStatus]:
	"""Coerce any status spelling into a canonical value.

	Case-insensitive and whitespace-tolerant. Returns `default` for
	unknown/empty input rather than raising -- callers here may be reading a
	row a future migration widened, and must not crash on a value they do not
	yet recognize.
	"""
	if isinstance(value, EpisodeStatus):
		return value
	if value is None:
		return default
	text = str(value).strip().lower()
	if not text:
		return default
	try:
		return EpisodeStatus(text)
	except ValueError:
		return default


def is_terminal(value: Union[str, EpisodeStatus, None]) -> bool:
	"""True when no further transition is expected for this episode.

	`exported` and `excluded` are both end states: an exported episode may be
	included in further cuts without changing its own status, and an excluded
	episode stays excluded (the exclusion decision is what is being recorded,
	not a placeholder pending reconsideration).
	"""
	return parse(value) in (EpisodeStatus.EXPORTED, EpisodeStatus.EXCLUDED)


def values(statuses: Iterable[EpisodeStatus] = PERSISTED_EPISODE_STATUSES) -> Tuple[str, ...]:
	"""The plain strings for `statuses` -- handy for SQL `IN (...)` rendering."""
	return tuple(s.value for s in statuses)
