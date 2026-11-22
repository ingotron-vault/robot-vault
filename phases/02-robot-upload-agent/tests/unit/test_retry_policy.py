"""The adaptive retry loop.

Covers the three things that decide whether retry is an asset or a liability:
which status codes get another attempt, the shape of the backoff, and the guard
that stops a lossy link from being flooded by full-file re-sends.
"""
import pytest

from robot_upload_agent import retry as R


class _Code:
	def __init__(self, name):
		self.name = name


class FakeRpcError(Exception):
	"""Duck-types grpc.RpcError: what matters is `.code().name`."""

	def __init__(self, name):
		super().__init__(name)
		self._name = name

	def code(self):
		return _Code(self._name)


def _run(codes, *, max_retries=3, file_size=0, base=1.0, mult=2.0):
	"""Fail with each code in `codes`, then succeed. Returns (result, outcome, slept)."""
	slept = []
	seq = list(codes)
	calls = {"n": 0}

	def fn(attempt):
		calls["n"] += 1
		if seq:
			raise FakeRpcError(seq.pop(0))
		return {"status": "completed", "attempt": attempt}

	result, outcome = R.call_with_retry(
		fn,
		max_retries=max_retries,
		base_backoff_seconds=base,
		retry_multiplier=mult,
		file_size_bytes=file_size,
		sleep=slept.append,
		rand=lambda: 1.0,  # deterministic: take the top of the jitter range
	)
	return result, outcome, slept, calls["n"]


# --- which codes retry -------------------------------------------------------

@pytest.mark.parametrize("code", sorted(R.RETRYABLE_STATUS_CODES - {"INTERNAL"}))
def test_transient_codes_are_retried_and_then_succeed(code):
	result, outcome, slept, calls = _run([code])
	assert result["status"] == "completed"
	assert calls == 2, "a retryable code must produce a second attempt"
	assert outcome.attempts == 2
	assert outcome.codes == [code]
	assert len(slept) == 1


@pytest.mark.parametrize("code", [
	"INVALID_ARGUMENT",      # chunk_too_small / chunk_hash_mismatch / sha256_mismatch
	"UNAUTHENTICATED",       # missing or wrong INGEST_AUTH_TOKEN
	"PERMISSION_DENIED",
	"FAILED_PRECONDITION",   # unregistered robot_id
	"NOT_FOUND",
	"UNIMPLEMENTED",
	"CANCELLED",
])
def test_deterministic_codes_are_not_retried(code):
	"""These fail identically on the second attempt, after re-sending the whole
	file. Retrying them is pure waste."""
	calls = {"n": 0}

	def fn(_attempt):
		calls["n"] += 1
		raise FakeRpcError(code)

	with pytest.raises(FakeRpcError):
		R.call_with_retry(
			fn, max_retries=5, base_backoff_seconds=0.0,
			sleep=lambda _s: pytest.fail("a non-retryable code must not sleep"),
		)
	assert calls["n"] == 1, f"{code} must be attempted exactly once"


def test_sha256_mismatch_is_not_retried():
	"""The server reports a hash mismatch as INVALID_ARGUMENT (main.rs
	`sha256_mismatch`). It is deterministic: the same bytes hash the same way, so
	a retry re-sends gigabytes for a guaranteed second failure."""
	calls = {"n": 0}

	def fn(_attempt):
		calls["n"] += 1
		raise FakeRpcError("INVALID_ARGUMENT")

	with pytest.raises(FakeRpcError):
		R.call_with_retry(fn, max_retries=3, base_backoff_seconds=0.0, sleep=lambda _s: None)
	assert calls["n"] == 1


def test_local_exception_without_a_status_code_is_not_retried():
	"""No gRPC status means a local failure (file vanished, bug in the chunk
	generator). Those reproduce exactly."""
	calls = {"n": 0}

	def fn(_attempt):
		calls["n"] += 1
		raise OSError("source file disappeared")

	with pytest.raises(OSError):
		R.call_with_retry(fn, max_retries=3, base_backoff_seconds=0.0, sleep=lambda _s: None)
	assert calls["n"] == 1


def test_internal_is_retried_but_capped():
	"""main.rs answers INTERNAL for the completed-row write failure *so that* the
	client retries, but a genuinely broken server also answers INTERNAL --
	so it is capped well under max_retries."""
	calls = {"n": 0}

	def fn(_attempt):
		calls["n"] += 1
		raise FakeRpcError("INTERNAL")

	with pytest.raises(FakeRpcError):
		R.call_with_retry(
			fn, max_retries=5, base_backoff_seconds=0.0, sleep=lambda _s: None,
		)
	assert calls["n"] == R.MAX_INTERNAL_ATTEMPTS


def test_max_retries_is_honoured():
	calls = {"n": 0}

	def fn(_attempt):
		calls["n"] += 1
		raise FakeRpcError("UNAVAILABLE")

	with pytest.raises(FakeRpcError):
		R.call_with_retry(
			fn, max_retries=2, base_backoff_seconds=0.0, sleep=lambda _s: None,
		)
	assert calls["n"] == 3, "max_retries=2 means one initial attempt plus two retries"


def test_max_retries_zero_means_no_retry():
	calls = {"n": 0}

	def fn(_attempt):
		calls["n"] += 1
		raise FakeRpcError("UNAVAILABLE")

	with pytest.raises(FakeRpcError):
		R.call_with_retry(fn, max_retries=0, base_backoff_seconds=1.0, sleep=lambda _s: None)
	assert calls["n"] == 1


# --- backoff shape -----------------------------------------------------------

def test_backoff_is_exponential_in_the_multiplier():
	# rand()==1.0 takes the top of the full-jitter range, exposing the nominal
	# value: base * multiplier ** attempt.
	top = [R.backoff_delay(n, 1.0, 2.0, rand=lambda: 1.0) for n in range(4)]
	assert top == [1.0, 2.0, 4.0, 8.0]
	# retry_multiplier comes from the connection strategy; POOR uses 3.0.
	assert R.backoff_delay(2, 0.5, 3.0, rand=lambda: 1.0) == pytest.approx(4.5)


def test_backoff_is_full_jitter_over_zero_to_nominal():
	"""Full jitter, not +/-20 %: the whole point is to de-synchronise a fleet that
	all lost the same server in the same instant."""
	assert R.backoff_delay(3, 1.0, 2.0, rand=lambda: 0.0) == 0.0
	assert R.backoff_delay(3, 1.0, 2.0, rand=lambda: 0.5) == pytest.approx(4.0)
	assert R.backoff_delay(3, 1.0, 2.0, rand=lambda: 1.0) == pytest.approx(8.0)
	# Every draw stays inside [0, nominal].
	for i in range(200):
		d = R.backoff_delay(2, 1.0, 2.0)
		assert 0.0 <= d <= 4.0


def test_backoff_is_capped():
	"""base=1, multiplier=3, attempt=6 is 729 s uncapped -- a robot stalled for 12
	minutes when it could have finished."""
	assert R.backoff_delay(6, 1.0, 3.0, rand=lambda: 1.0) == R.MAX_BACKOFF_SECONDS
	assert R.backoff_delay(6, 1.0, 3.0, rand=lambda: 0.5) == R.MAX_BACKOFF_SECONDS / 2


def test_resource_exhausted_backs_off_harder():
	"""RESOURCE_EXHAUSTED is the server's per-robot/global semaphore saying "slow
	down"; racing back in at the same cadence is how a fleet turns backpressure
	into a stampede."""
	plain = R.backoff_delay(0, 1.0, 2.0, "UNAVAILABLE", rand=lambda: 1.0)
	penalised = R.backoff_delay(0, 1.0, 2.0, "RESOURCE_EXHAUSTED", rand=lambda: 1.0)
	assert penalised > plain
	assert penalised == pytest.approx(plain * R.BACKOFF_PENALTY["RESOURCE_EXHAUSTED"])


def test_delays_grow_across_a_real_retry_sequence():
	_result, outcome, slept, _calls = _run(["UNAVAILABLE", "UNAVAILABLE", "UNAVAILABLE"], max_retries=3)
	assert slept == [1.0, 2.0, 4.0]
	assert outcome.delays == slept


# --- the byte budget ---------------------------------------------------------

def test_byte_budget_abandons_before_max_retries_on_a_big_file():
	"""Resume restarts from zero, so every failed attempt re-sends the whole
	file. Attempt count alone is not a safe bound: 5 retries of a 500 MB file is
	3 GB of uplink. The budget is K x file_size."""
	calls = {"n": 0}
	size = 500 * 1024 * 1024

	def fn(_attempt):
		calls["n"] += 1
		raise FakeRpcError("UNAVAILABLE")

	with pytest.raises(FakeRpcError):
		R.call_with_retry(
			fn, max_retries=10, base_backoff_seconds=0.0,
			file_size_bytes=size, sleep=lambda _s: None,
		)
	# K=3 permits the initial send plus two re-sends.
	assert calls["n"] == int(R.MAX_RESENT_BYTES_MULTIPLIER)


def test_unknown_file_size_falls_back_to_the_attempt_count():
	calls = {"n": 0}

	def fn(_attempt):
		calls["n"] += 1
		raise FakeRpcError("UNAVAILABLE")

	with pytest.raises(FakeRpcError):
		R.call_with_retry(
			fn, max_retries=2, base_backoff_seconds=0.0,
			file_size_bytes=0, sleep=lambda _s: None,
		)
	assert calls["n"] == 3


def test_success_on_the_first_attempt_costs_nothing():
	result, outcome, slept, calls = _run([])
	assert result["status"] == "completed"
	assert calls == 1
	assert outcome.attempts == 1
	assert outcome.codes == []
	assert slept == []


@pytest.mark.parametrize("sent", [0, 10])
def test_probe_or_small_suffix_failures_use_actual_bytes_and_can_keep_retrying(sent):
	def attempt(number):
		if number < 5:
			raise FakeRpcError("UNAVAILABLE")
		return {"status": "completed"}
	result, outcome = R.call_with_retry(
		attempt, max_retries=5, base_backoff_seconds=0,
		file_size_bytes=1000, attempt_bytes=lambda: sent,
	)
	assert result["status"] == "completed"
	assert outcome.attempts == 6
	assert outcome.bytes_resent == 5 * sent


def test_actual_retransmitted_traffic_exhausts_the_byte_budget():
	calls = []
	def attempt(number):
		calls.append(number)
		raise FakeRpcError("UNAVAILABLE")
	with pytest.raises(FakeRpcError):
		R.call_with_retry(
			attempt, max_retries=10, base_backoff_seconds=0,
			file_size_bytes=1000, attempt_bytes=lambda: 1000,
		)
	assert calls == [0, 1, 2]
