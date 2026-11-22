"""Application-level upload retry with full-jitter backoff.

WHY THIS IS HAND-ROLLED AND NOT A gRPC `retryPolicy`
----------------------------------------------------
A gRPC call becomes *committed* -- no further transport-level retry, whatever
the status code or the attempts remaining -- as soon as the client sees any
response header, or as soon as outgoing streamed messages exceed the client's
retry buffer. An MCAP upload is one long-lived client-streaming RPC of many
megabytes, so it is committed almost immediately. Configuring a service-config
`retryPolicy` for `Upload` would buffer bytes and then silently stop applying,
which is worse than nothing: it costs memory and delivers no retries.

Retry lives above the transport. Each attempt probes the server's durable
parts and starts a fresh RPC bound to that session. New peers send only missing
parts; old peers still restart the file. Session tokens, the server upload lease,
and immutable metadata validation make ambiguous failures safe to retry.

The loop retains bounded backoff and attempt limits. Its byte budget uses the
actual bytes yielded to failed attempts when the caller can measure them, so a
failed resume probe or a small remaining suffix does not cost a full file's
budget. Other callers keep conservative whole-file accounting.
"""
import random
import time as _time
from dataclasses import dataclass
from typing import Callable, Optional, Set

#: gRPC status codes worth another attempt.
#:
#: UNAVAILABLE          textbook transient: connection refused/reset, server
#:                      restarting, LB draining.
#: DEADLINE_EXCEEDED    ambiguous in general (the server may have partially
#:                      executed) but safe here: every new RPC re-probes durable
#:                      parts, binds the plan to a session token, and the server
#:                      serializes attempts under its upload lease. A completed
#:                      attempt replays its receipt after metadata validation.
#: RESOURCE_EXHAUSTED   backpressure from the ingest server's per-robot/global
#:                      semaphores. Retry, but slower -- see BACKOFF_PENALTY.
#: ABORTED              concurrency conflict; the correct response is to restart
#:                      the whole operation, which is exactly what an attempt is.
#: INTERNAL             main.rs deliberately answers `Status::internal` when the
#:                      completed-row write fails *so that the client
#:                      retries*; the object is stored but unrecorded, and a
#:                      replay is what reconciles it. Bounded hard by
#:                      MAX_INTERNAL_ATTEMPTS because a genuinely broken server
#:                      also answers INTERNAL and must not be hammered.
#:
#: Deliberately NOT retried, because they are deterministic -- the second attempt
#:  fails identically after re-sending the whole file:
#:   INVALID_ARGUMENT     chunk_too_small, chunk_hash_mismatch, sha256_mismatch
#:   UNAUTHENTICATED      missing/wrong INGEST_AUTH_TOKEN
#:   PERMISSION_DENIED    authorised but not allowed
#:   FAILED_PRECONDITION  unregistered robot_id -- needs an operator
#:   NOT_FOUND / UNIMPLEMENTED   wrong server or wrong contract
#:   CANCELLED            we cancelled it; retrying overrides the caller
#:   ALREADY_EXISTS / OUT_OF_RANGE / DATA_LOSS   state, not weather
RETRYABLE_STATUS_CODES: Set[str] = frozenset({
	"UNAVAILABLE",
	"DEADLINE_EXCEEDED",
	"RESOURCE_EXHAUSTED",
	"ABORTED",
	"INTERNAL",
})

#: INTERNAL is retryable but only just; cap it well below max_retries.
MAX_INTERNAL_ATTEMPTS: int = 2
#: RESOURCE_EXHAUSTED means the server told us to slow down, so multiply the
#: computed delay rather than racing back in at the same cadence.
BACKOFF_PENALTY = {"RESOURCE_EXHAUSTED": 4.0}
#: Absolute ceiling on one sleep. With retry_multiplier=3.0 (the POOR strategy)
#: attempt 4 is base x 81, which uncapped is a 20-minute stall on a robot that
#: could have finished.
MAX_BACKOFF_SECONDS: float = 60.0
#: Give up once cumulative re-sent bytes exceed K x file_size, even with attempts
#: left. K=3 allows at most three file sizes of failed-attempt traffic.
#: A caller without byte measurements conservatively charges the whole file.
MAX_RESENT_BYTES_MULTIPLIER: float = 3.0


def status_code_name(exc: BaseException) -> Optional[str]:
	"""The gRPC status code name carried by `exc`, or None if it carries none.

	Duck-typed on purpose: `grpc.RpcError` instances expose `.code()`, and so do
	the fakes in the unit tests, which keeps this module importable (and
	testable) without grpcio installed.
	"""
	code = getattr(exc, "code", None)
	if not callable(code):
		return None
	try:
		value = code()
	except Exception:  # pragma: no cover - a broken fake, not our problem
		return None
	name = getattr(value, "name", None)
	return str(name) if name else str(value)


def is_retryable(exc: BaseException, attempts_by_code: Optional[dict] = None) -> bool:
	"""Is another attempt worth the bytes it will cost?

	An exception with no gRPC status code is NOT retried. Those are local
	failures -- the file vanished, the manifest is unreadable, a bug in the
	chunk generator -- and they reproduce exactly.
	"""
	name = status_code_name(exc)
	if name is None or name not in RETRYABLE_STATUS_CODES:
		return False
	if name == "INTERNAL":
		seen = (attempts_by_code or {}).get("INTERNAL", 0)
		return seen < MAX_INTERNAL_ATTEMPTS
	return True


def backoff_delay(
	attempt: int,
	base_backoff_seconds: float,
	retry_multiplier: float,
	code_name: Optional[str] = None,
	rand: Optional[Callable[[], float]] = None,
) -> float:
	"""Full-jitter exponential backoff: `uniform(0, min(cap, base * mult**attempt))`.

	`attempt` is 0-based (0 = the delay after the first failure).

	Full jitter, hand-rolled, because the two authorities disagree on the shape:
	gRPC's A6 proposal specifies a bounded +/-20 % jitter around the nominal
	backoff, while Microsoft's gRPC retry documentation describes the delay as
	uniform over [0, current_backoff). Those are different distributions. Full
	jitter is the one that actually de-synchronises a fleet of robots that all
	lost the same server at the same instant, so it is the one we own here
	rather than inherit.
	"""
	rand = rand or random.random
	nominal = float(base_backoff_seconds) * (float(retry_multiplier) ** max(0, int(attempt)))
	nominal *= BACKOFF_PENALTY.get(code_name or "", 1.0)
	ceiling = min(MAX_BACKOFF_SECONDS, max(0.0, nominal))
	return ceiling * rand()


@dataclass
class RetryOutcome:
	"""What the retry loop did, for logging and for the caller's error payload."""
	attempts: int = 1
	delays: Optional[list] = None
	codes: Optional[list] = None
	bytes_resent: int = 0
	abandoned_reason: Optional[str] = None

	def __post_init__(self) -> None:
		self.delays = self.delays if self.delays is not None else []
		self.codes = self.codes if self.codes is not None else []


def call_with_retry(
	fn: Callable[[int], dict],
	*,
	max_retries: int,
	base_backoff_seconds: float,
	retry_multiplier: float = 2.0,
	file_size_bytes: int = 0,
	sleep: Callable[[float], None] = _time.sleep,
	rand: Optional[Callable[[], float]] = None,
	on_retry: Optional[Callable[[int, str, float], None]] = None,
	attempt_bytes: Optional[Callable[[], int]] = None,
) -> tuple:
	"""Run `fn(attempt)` until it succeeds, is non-retryable, or runs out of budget.

	`fn` receives the 0-based attempt number and re-probes the durable resume
	plan before each fresh RPC. When provided, `attempt_bytes` reports bytes
	yielded to the failed RPC; otherwise budgeting conservatively charges one
	whole file per failed attempt.

	Returns `(result, RetryOutcome)`. Re-raises the last exception when every
	attempt failed, so callers keep their existing except-block behaviour.
	"""
	outcome = RetryOutcome(attempts=0)
	attempts_by_code: dict = {}
	budget = MAX_RESENT_BYTES_MULTIPLIER * float(file_size_bytes or 0)
	last_exc: Optional[BaseException] = None

	for attempt in range(max(0, int(max_retries)) + 1):
		outcome.attempts = attempt + 1
		try:
			return fn(attempt), outcome
		except BaseException as exc:  # noqa: BLE001 - re-raised below
			last_exc = exc
			name = status_code_name(exc) or "no_status"
			outcome.codes.append(name)
			attempts_by_code[name] = attempts_by_code.get(name, 0) + 1
			if not is_retryable(exc, attempts_by_code):
				outcome.abandoned_reason = f"non_retryable:{name}"
				raise
			if attempt >= int(max_retries):
				outcome.abandoned_reason = f"max_retries_exhausted:{name}"
				raise
			charged_bytes = int(file_size_bytes or 0)
			if attempt_bytes is not None:
				try:
					measured = attempt_bytes()
					if type(measured) is int and measured >= 0:
						charged_bytes = measured
				except Exception:
					pass  # Preserve the original upload failure and conservative budget.
			outcome.bytes_resent += charged_bytes
			if budget and outcome.bytes_resent >= budget:
				outcome.abandoned_reason = (
					f"byte_budget_exhausted:{outcome.bytes_resent}>="
					f"{int(budget)}"
				)
				raise
			delay = backoff_delay(attempt, base_backoff_seconds, retry_multiplier, name, rand)
			outcome.delays.append(delay)
			if callable(on_retry):
				on_retry(attempt, name, delay)
			if delay > 0:
				sleep(delay)

	raise last_exc if last_exc else RuntimeError("call_with_retry made no attempt")
