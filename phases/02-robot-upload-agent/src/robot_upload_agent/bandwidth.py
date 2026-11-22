"""Process-local, queue-shared pacing for upload payload bytes."""

import threading
import time
from typing import Callable, Optional


class UploadCancelled(Exception):
	"""The queue was stopped while waiting for upload bandwidth."""


class BandwidthLimiter:
	"""Reserve payload bytes against one aggregate binary-Mbit/s allowance.

	A reservation is made immediately before handing bytes to a transport. The
	initial burst is at most one second of allowance; larger RPC messages wait
	for their excess before dispatch. Reservations from all worker threads share
	one timeline. Separate processes have separate timelines.
	"""

	def __init__(
		self,
		mbps: int,
		*,
		clock: Callable[[], float] = time.monotonic,
		wait: Optional[Callable[[float], None]] = None,
		cancel_event: Optional[threading.Event] = None,
	) -> None:
		if mbps < 0:
			raise ValueError("bandwidth_mbps must be nonnegative")
		self.bytes_per_second = mbps * 1024 * 1024 / 8
		self.burst_bytes = self.bytes_per_second
		self._clock = clock
		self._cancel = cancel_event or threading.Event()
		self._wait = wait or self._cancel.wait
		self._lock = threading.Lock()
		self._next_available = clock()

	def acquire(self, byte_count: int) -> None:
		if byte_count < 0:
			raise ValueError("byte_count must be nonnegative")
		if self._cancel.is_set():
			raise UploadCancelled("upload cancelled while waiting for bandwidth")
		if not byte_count or not self.bytes_per_second:
			return
		with self._lock:
			now = self._clock()
			self._next_available = max(self._next_available, now) + byte_count / self.bytes_per_second
			deadline = self._next_available - self.burst_bytes / self.bytes_per_second
		while True:
			if self._cancel.is_set():
				raise UploadCancelled("upload cancelled while waiting for bandwidth")
			remaining = deadline - self._clock()
			if remaining <= 0:
				return
			self._wait(min(remaining, 0.1))

	def cancel(self) -> None:
		self._cancel.set()
