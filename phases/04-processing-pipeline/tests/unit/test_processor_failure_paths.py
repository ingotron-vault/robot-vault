"""
Unit tests for processor per-upload failure isolation and
honest bookkeeping.

Covers the processor's four guarded failure modes:
  (a) a per-upload exception must not abandon the rest of the batch, must
      be recorded as a failed attempt with an incrementing attempt count,
      and the upload must stop being re-selected once attempts reach
      PROCESSOR_MAX_ATTEMPTS ('dead_letter').
  (b) a failed 'processed' bookkeeping insert must NOT be counted as a
      processed upload (no silent infinite reprocessing with lying
      success metrics).
  (c) a nonexistent local storage_path must be treated as a failure (and
      retried), not silently marked processed with rows_written=0.
  (d) an upload whose CDR-encoded messages are ~100% undecodable must
      increment a schema-labeled decode-failure counter and be
      flagged/failed rather than silently written with all-null fields.

Follows the existing test style (phases/04-processing-pipeline/tests/unit/
test_processing_core.py): mocks psycopg and mcap.reader.make_reader, no
real database or MCAP file content.
"""
from pathlib import Path
from unittest.mock import patch, Mock

import pytest
from prometheus_client import REGISTRY

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor.core import (
    ProcessorConfig,
    PROCESSOR_MAX_ATTEMPTS,
    DecodeFailureError,
    _extract_minimal_topics_to_parquet,
    run_once,
)
from processor.cdr_decode import CDR_DECODE_FAILURES


def _config(tmp_path: Path) -> ProcessorConfig:
    return ProcessorConfig(
        parquet_root=tmp_path / "parquet",
        topics=[],
    )


def _fake_candidate_source(candidates):
    """A `list_unprocessed_uploads` stand-in that honours the claim protocol.

    `run_once` no longer takes one 50-row batch: when a DSN is configured it
    claims a single upload at a time and passes back the ids it has already
    attempted, so that a healthy replica can pick up the rest and no claim
    expires waiting its turn in a long batch. A stub pinned with
    `return_value=` ignores both `limit` and `exclude_upload_ids` and so hands
    the same rows back on every pass, which turns that loop into
    PROCESSOR_BATCH_LIMIT repeats of the whole fixture (the "called 50 times"
    and `100 == 2` failures). The real query applies LIMIT and a
    `NOT (client_upload_id = ANY(...))` predicate; this mirrors exactly that,
    so the same assertions hold on both the Postgres claim path and the
    single-list local-mode seam.
    """

    claims = [_claim(row) for row in candidates]

    def _list(limit: int = 50, *, exclude_upload_ids=None):
        excluded = set(exclude_upload_ids or ())
        remaining = [row for row in claims if row[0] not in excluded]
        return remaining[:limit]

    return _list


def _claim(row):
    """A five-column claim row (upload, robot, path, attempt UUID, attempt)."""
    return tuple(row) + ("00000000-0000-0000-0000-0000000000a1", 1)


@pytest.fixture(autouse=True)
def _claimed_runtime(monkeypatch):
    """run_once only claims with Postgres configured; fake the claim plumbing."""
    from processor import core

    monkeypatch.setattr(core, "_get_pg_conn_str", lambda: "postgres://fake")
    monkeypatch.setattr(core, "_pg_prepare_claim", Mock())
    monkeypatch.setattr(core, "_pg_renew_claim", Mock(return_value=True))
    monkeypatch.setattr(core, "_pg_register_parquet_file", Mock())
    monkeypatch.setattr(core, "_pg_dualwrite_processed_topic", Mock())
    monkeypatch.setenv("SEGMENTATION_ENABLED", "false")


# ---------------------------------------------------------------------------
# (a) one poison upload raising does not stop the batch, and is excluded
#     from future candidate lists once it exceeds PROCESSOR_MAX_ATTEMPTS.
# ---------------------------------------------------------------------------

class TestBatchIsolation:
    def test_poison_upload_does_not_stop_batch(self, tmp_path):
        """3 candidates, #1 raises -- #2 and #3 must still be processed,
        and #1 must be recorded as a failed attempt (not re-raised)."""
        candidates = [
            ("upload1", "robot1", "/fake/upload1.mcap"),
            ("upload2", "robot1", "/fake/upload2.mcap"),
            ("upload3", "robot1", "/fake/upload3.mcap"),
        ]

        def fake_extract(cfg, upload_id, robot_id, storage_path, *, stats=None, **_kwargs):
            if upload_id == "upload1":
                raise RuntimeError("simulated poison upload (corrupt MCAP)")
            if stats is not None:
                stats.update(messages_read=5, messages_skipped_by_schema={}, rows_written=5)
            return 5

        with patch("processor.core.list_unprocessed_uploads", side_effect=_fake_candidate_source(candidates)), \
             patch("processor.core._extract_minimal_topics_to_parquet", side_effect=fake_extract), \
             patch("processor.core._pg_complete_claim") as mock_complete, \
             patch("processor.core._pg_record_failure") as mock_record_failure:
            mock_record_failure.return_value = 1

            processed_count = run_once(_config(tmp_path))

        # The batch was NOT abandoned: both healthy uploads were processed.
        assert processed_count == 2
        assert mock_complete.call_count == 2
        processed_ids = {call.args[0] for call in mock_complete.call_args_list}
        assert processed_ids == {"upload2", "upload3"}

        # The poison upload was recorded as a failure, not silently dropped
        # and not left to crash the loop via `raise`.
        mock_record_failure.assert_called_once()
        failed_upload_id, error_message = mock_record_failure.call_args.args[0], mock_record_failure.call_args.args[1]
        assert failed_upload_id == "upload1"
        assert "RuntimeError" in error_message

    def test_poison_upload_excluded_after_max_attempts(self, tmp_path):
        """A single upload that always raises must be retried up to
        PROCESSOR_MAX_ATTEMPTS times, then stop being re-selected --
        simulating list_unprocessed_uploads/​_pg_record_failure's shared
        Postgres state with a tiny in-memory fake so this stays a unit
        test (no real DB) while still exercising the real exclusion
        contract end-to-end via run_once."""

        class FakeStore:
            def __init__(self):
                self.attempts = 0
                self.dead_letter = False

            def list_candidates(self, limit=50, *, exclude_upload_ids=None):
                if self.dead_letter or "poison" in set(exclude_upload_ids or ()):
                    return []
                return [_claim(("poison", "robot1", "/fake/poison.mcap"))][:limit]

            def record_failure(self, upload_id, error_message, metadata=None, **_kwargs):
                self.attempts += 1
                if self.attempts >= PROCESSOR_MAX_ATTEMPTS:
                    self.dead_letter = True
                return self.attempts

        store = FakeStore()

        def always_raises(cfg, upload_id, robot_id, storage_path, *, stats=None, **_kwargs):
            raise RuntimeError("permanently corrupt MCAP")

        with patch("processor.core.list_unprocessed_uploads", side_effect=store.list_candidates), \
             patch("processor.core._extract_minimal_topics_to_parquet", side_effect=always_raises), \
             patch("processor.core._pg_complete_claim"), \
             patch("processor.core._pg_record_failure", side_effect=store.record_failure):
            # Run PROCESSOR_MAX_ATTEMPTS times: the poison upload is
            # selected and fails every time, incrementing attempts.
            for _ in range(PROCESSOR_MAX_ATTEMPTS):
                processed_count = run_once(_config(tmp_path))
                assert processed_count == 0

            assert store.attempts == PROCESSOR_MAX_ATTEMPTS
            assert store.dead_letter is True

            # One more run: the now-dead-lettered upload must no longer
            # be selected at all (list_unprocessed_uploads' job).
            processed_count = run_once(_config(tmp_path))
            assert processed_count == 0
            assert store.attempts == PROCESSOR_MAX_ATTEMPTS  # unchanged -- never retried again


# ---------------------------------------------------------------------------
# (b) a failed 'processed' insert must not count the upload as processed.
# ---------------------------------------------------------------------------

class TestFailedProcessedInsert:
    def test_failed_processed_insert_not_counted_processed(self, tmp_path):
        candidates = [("upload1", "robot1", "/fake/upload1.mcap")]

        def fake_extract(cfg, upload_id, robot_id, storage_path, *, stats=None, **_kwargs):
            if stats is not None:
                stats.update(messages_read=5, messages_skipped_by_schema={}, rows_written=5)
            return 5

        with patch("processor.core.list_unprocessed_uploads", side_effect=_fake_candidate_source(candidates)), \
             patch("processor.core._extract_minimal_topics_to_parquet", side_effect=fake_extract), \
             patch("processor.core._pg_complete_claim", side_effect=RuntimeError("Postgres insert into 'processed' failed")) as mock_complete, \
             patch("processor.core._pg_record_failure") as mock_record_failure, \
             patch("processor.core.PROCESSED_UPLOADS") as mock_metric:
            mock_record_failure.return_value = 1

            processed_count = run_once(_config(tmp_path))

        # The insert into `processed` was attempted, and failed.
        mock_complete.assert_called_once()
        # The upload must NOT be counted processed: neither the return
        # value nor the success-metric increment may reflect success.
        assert processed_count == 0
        mock_metric.inc.assert_not_called()
        # It IS recorded as a failed attempt, so it gets retried.
        mock_record_failure.assert_called_once()
        assert mock_record_failure.call_args.args[0] == "upload1"


# ---------------------------------------------------------------------------
# (c) a nonexistent storage_path is a failure, not processed-with-0-rows.
# ---------------------------------------------------------------------------

class TestNonexistentStoragePath:
    def test_nonexistent_local_path_raises(self, tmp_path):
        """_extract_minimal_topics_to_parquet must raise (not `return 0`)
        for a local storage_path that does not exist on disk."""
        config = _config(tmp_path)
        missing_path = str(tmp_path / "does_not_exist.mcap")
        assert not Path(missing_path).exists()

        with pytest.raises(FileNotFoundError):
            _extract_minimal_topics_to_parquet(config, "upload_missing", "robot1", missing_path, attempt_id="00000000-0000-0000-0000-0000000000a1")

    def test_nonexistent_path_is_recorded_failed_via_run_once(self, tmp_path):
        """End-to-end through run_once: a missing object is retried, and
        is never written to `processed` or marked 'completed'/'completed_empty'."""
        missing_path = str(tmp_path / "does_not_exist.mcap")
        candidates = [("upload_missing", "robot1", missing_path)]

        with patch("processor.core.list_unprocessed_uploads", side_effect=_fake_candidate_source(candidates)), \
             patch("processor.core._pg_complete_claim") as mock_complete, \
             patch("processor.core._pg_record_failure") as mock_record_failure:
            mock_record_failure.return_value = 1

            processed_count = run_once(_config(tmp_path))

        assert processed_count == 0
        mock_complete.assert_not_called()
        mock_record_failure.assert_called_once()
        failed_upload_id, error_message = mock_record_failure.call_args.args[0], mock_record_failure.call_args.args[1]
        assert failed_upload_id == "upload_missing"
        assert "FileNotFoundError" in error_message


# ---------------------------------------------------------------------------
# (d) an all-decode-failure upload increments the decode-failure counter
#     and is flagged (raises DecodeFailureError / recorded failed).
# ---------------------------------------------------------------------------

class TestDecodeFailureFlagging:
    def _sample(self, schema: str) -> float:
        return REGISTRY.get_sample_value(
            "processor_cdr_decode_failures_total", {"schema": schema}
        ) or 0.0

    def test_all_decode_failures_increments_counter_and_raises(self, tmp_path):
        schema_name = "sensor_msgs/msg/Imu"
        mock_schema = Mock()
        mock_schema.name = schema_name
        mock_channel = Mock()
        mock_channel.topic = "/robot/imu"
        mock_channel.message_encoding = "cdr"

        mock_messages = []
        for i in range(5):
            msg = Mock()
            msg.log_time = 1_000_000_000 + i * 1_000_000
            msg.data = b"undecodable_cdr_bytes_%d" % i
            mock_messages.append((mock_schema, mock_channel, msg))

        mcap_file = tmp_path / "all_decode_fail.mcap"
        mcap_file.write_bytes(b"dummy mcap data")

        before = self._sample(schema_name)

        # The ratio gate is fail-closed for malformed CDR schemas/bytes; this
        # test exercises an otherwise healthy dynamic decoder runtime whose
        # payloads still fail to decode.
        with patch("processor.core.check_cdr_decoder_available", return_value=True), \
             patch("processor.core.make_reader") as mock_reader:
            mock_reader.return_value.iter_messages.return_value = mock_messages

            with pytest.raises(DecodeFailureError):
                _extract_minimal_topics_to_parquet(
                    _config(tmp_path), "upload_decode_fail", "robot1", str(mcap_file),
                    attempt_id="00000000-0000-0000-0000-0000000000a1",
                )

        after = self._sample(schema_name)
        assert after - before == 5, "CDR_DECODE_FAILURES must increment once per failed decode"

    def test_decode_failure_flags_upload_via_run_once(self, tmp_path):
        """End-to-end: run_once must record the all-decode-failure upload
        as a failed attempt, not as processed."""
        schema_name = "sensor_msgs/msg/Imu"
        mock_schema = Mock()
        mock_schema.name = schema_name
        mock_channel = Mock()
        mock_channel.topic = "/robot/imu"
        mock_channel.message_encoding = "cdr"
        msg = Mock()
        msg.log_time = 1_000_000_000
        msg.data = b"undecodable"
        mock_messages = [(mock_schema, mock_channel, msg)]

        mcap_file = tmp_path / "decode_fail.mcap"
        mcap_file.write_bytes(b"dummy mcap data")

        candidates = [("upload_decode_fail", "robot1", str(mcap_file))]

        with patch("processor.core.list_unprocessed_uploads", side_effect=_fake_candidate_source(candidates)), \
             patch("processor.core.check_cdr_decoder_available", return_value=True), \
             patch("processor.core.make_reader") as mock_reader, \
             patch("processor.core._pg_complete_claim") as mock_complete, \
             patch("processor.core._pg_record_failure") as mock_record_failure:
            mock_reader.return_value.iter_messages.return_value = mock_messages
            mock_record_failure.return_value = 1

            processed_count = run_once(_config(tmp_path))

        assert processed_count == 0
        mock_complete.assert_not_called()
        mock_record_failure.assert_called_once()
        failed_upload_id, error_message = mock_record_failure.call_args.args[0], mock_record_failure.call_args.args[1]
        assert failed_upload_id == "upload_decode_fail"
        assert "DecodeFailureError" in error_message


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
