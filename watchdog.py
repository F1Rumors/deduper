"""
watchdog.py — Per-operation hang detector and phase timer for NAS I/O.

``Watchdog`` runs a daemon thread that monitors a deadline set by the caller
before each potentially-blocking operation.  If the deadline is exceeded it
prints a diagnostic message to stderr and calls ``os._exit(1)`` to hard-kill
the process, bypassing Python cleanup so that a stuck kernel read does not
prevent exit.

It also records per-operation timings via an embedded ``PhaseTimer`` so
callers can report outliers after the phase completes.

Typical usage::

    with Watchdog(default_timeout=cfg.hash_timeout) as wd:
        for item in work_items:
            wd.arm(f"processing {item}")
            try:
                do_slow_nas_thing(item)
            finally:
                wd.disarm()
    _report_phase_timings("Phase name", wd)

For ``imap_unordered`` loops, arm before the first result then re-arm on each
received result to create a per-item heartbeat::

    wd.arm(f"waiting for first result ({n} items in pool)")
    for result in pool.imap_unordered(worker, items):
        wd.arm(f"waiting for next result ({n - done} remaining, last: {result})")
        ...
    wd.disarm()
"""

from __future__ import annotations

import heapq
import os
import sys
import threading
import time
from typing import Callable, Optional


class PhaseTimer:
    """Welford's online mean/variance tracker with a top-K heap of slowest samples.

    Records operation timings incrementally (O(1) space for statistics;
    O(K) space for the top-K sample heap).  After a phase completes, call
    ``outliers()`` to get samples that are both above an absolute threshold
    and above ``mean + sigma * stddev``.

    :param threshold_ms: Absolute floor for outlier reporting (milliseconds).
    :param sigma:        How many standard deviations above the mean qualifies
                         as an outlier.
    :param top_k:        Maximum number of slow samples to retain.
    """

    def __init__(
        self,
        threshold_ms: float = 4000.0,
        sigma: float = 3.0,
        top_k: int = 20,
    ) -> None:
        self._threshold_ms = threshold_ms
        self._sigma = sigma
        self._top_k = top_k
        self._n = 0
        self._mean = 0.0
        self._M2 = 0.0
        self._heap: list[tuple[float, str]] = []  # min-heap (ms, label)

    def record(self, elapsed_ms: float, label: str) -> None:
        """Record one timing sample."""
        self._n += 1
        delta = elapsed_ms - self._mean
        self._mean += delta / self._n
        self._M2 += delta * (elapsed_ms - self._mean)

        if len(self._heap) < self._top_k:
            heapq.heappush(self._heap, (elapsed_ms, label))
        elif elapsed_ms > self._heap[0][0]:
            heapq.heapreplace(self._heap, (elapsed_ms, label))

    @property
    def n(self) -> int:
        return self._n

    @property
    def mean_ms(self) -> float:
        return self._mean if self._n > 0 else 0.0

    @property
    def stddev_ms(self) -> float:
        if self._n < 2:
            return 0.0
        return (self._M2 / (self._n - 1)) ** 0.5

    def outliers(self) -> list[tuple[float, str]]:
        """Return top-K samples exceeding both the absolute threshold and mean+N*σ.

        Returns an empty list when fewer than two samples have been recorded
        (stddev is undefined).  Results are sorted slowest-first.
        """
        if self._n < 2:
            return []
        cutoff = max(self._threshold_ms, self._mean + self._sigma * self.stddev_ms)
        return sorted(
            [(ms, lbl) for ms, lbl in self._heap if ms >= cutoff],
            reverse=True,
        )


class Watchdog:
    """Daemon-thread hang detector for blocking NAS I/O.

    Call :meth:`arm` with a description before each potentially-blocking
    operation and :meth:`disarm` immediately after.  If the operation does not
    complete within the configured timeout the watchdog flushes stdout, prints
    a diagnostic to stderr, and calls ``os._exit(1)``.

    Each :meth:`arm`/:meth:`disarm` pair (and each re-arm while already armed)
    records an elapsed-time sample to the embedded :attr:`timer` so callers
    can report per-phase outliers after the phase completes.

    :param default_timeout: Seconds before the watchdog fires (default 10).
    :param _exit_fn:        Replacement for ``os._exit`` — for unit tests only.
    """

    _CHECK_INTERVAL: float = 0.5  # seconds between watchdog ticks

    def __init__(
        self,
        default_timeout: float = 10.0,
        _exit_fn: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._default_timeout = default_timeout
        self._exit_fn: Callable[[int], None] = (
            _exit_fn if _exit_fn is not None else os._exit
        )
        self._lock = threading.Lock()
        self._armed = False
        self._deadline = 0.0
        self._reason = ""
        self._arm_time: Optional[float] = None
        self._current_timeout: float = default_timeout
        self._stop = threading.Event()
        self._timer = PhaseTimer()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="Watchdog"
        )
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────

    def arm(self, reason: str, timeout: Optional[float] = None) -> None:
        """Arm the watchdog for an operation described by *reason*.

        If the watchdog is not :meth:`disarm`\\ed within *timeout* seconds it
        fires.  Re-arming while already armed records the previous arm's elapsed
        to the phase timer and resets the deadline — call this at each iteration
        of a loop to create a per-item heartbeat.
        """
        now = time.monotonic()
        effective = timeout if timeout is not None else self._default_timeout
        deadline = now + effective
        with self._lock:
            if self._armed and self._arm_time is not None:
                elapsed_ms = (now - self._arm_time) * 1000.0
                self._timer.record(elapsed_ms, self._reason)
            self._reason = reason
            self._deadline = deadline
            self._current_timeout = effective
            self._armed = True
            self._arm_time = now

    def disarm(self) -> None:
        """Disarm the watchdog — the guarded operation completed in time."""
        now = time.monotonic()
        with self._lock:
            if self._armed and self._arm_time is not None:
                elapsed_ms = (now - self._arm_time) * 1000.0
                self._timer.record(elapsed_ms, self._reason)
            self._armed = False
            self._arm_time = None

    def stop(self) -> None:
        """Stop the watchdog thread.  Called automatically by the context manager."""
        self._stop.set()
        self._thread.join(timeout=2.0)

    @property
    def timer(self) -> PhaseTimer:
        """The phase timer accumulating per-operation timings for this watchdog."""
        return self._timer

    # ── Context manager ────────────────────────────────────────────────────

    def __enter__(self) -> "Watchdog":
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ── Internal ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.wait(timeout=self._CHECK_INTERVAL):
            with self._lock:
                fired = self._armed and time.monotonic() > self._deadline
                reason = self._reason if fired else ""
            if not fired:
                continue
            sys.stdout.flush()
            print(
                f"\nFatal: watchdog timeout ({self._current_timeout:.0f}s) — {reason}",
                file=sys.stderr,
                flush=True,
            )
            self._exit_fn(1)
            return  # only reached when _exit_fn is mocked in tests
