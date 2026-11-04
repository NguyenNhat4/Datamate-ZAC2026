"""Deadlines shared by model calls, worker actions, and question cleanup."""
from contextlib import contextmanager
from dataclasses import dataclass
import signal
import threading
import time


class QuestionStopped(BaseException):
    """Stop native framework retries for a terminal question failure.

    BaseException is intentional: framework handlers catch Exception and retry
    ordinary model/code errors. A timeout or broken worker must escape them.
    """


@dataclass(frozen=True)
class Deadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        if seconds <= 0:
            raise ValueError("Deadline duration must be positive")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        seconds = self.expires_at - time.monotonic()
        if seconds <= 0:
            raise QuestionStopped("Question deadline exhausted")
        return seconds


@contextmanager
def enforce_deadline(deadline: Deadline):
    """Interrupt active synchronous work, including a stalled model request."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Question execution must run on the main thread")

    def expired(signum, frame):
        raise QuestionStopped("Question deadline exhausted")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, deadline.remaining())
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            remaining = max(0.000001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])
