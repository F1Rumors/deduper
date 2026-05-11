"""
watchdog.py — Per-operation hang detector for NAS I/O.

The Watchdog runs a daemon thread that monitors a deadline set by the caller
before each potentially-blocking operation.  If the deadline is exceeded it
prints a diagnostic message to stderr and calls ``os._exit(1)`` to hard-kill
the process, bypassing Python cleanup so that a stuck kernel read does not
prevent exit.

Typical usage::

    with Watchdog(default_timeout=cfg.hash_timeout) as wd:
        for item in work_items:
            wd.arm(f"processing {item}")
            try:
                do_slow_nas_thing(item)
            finally:
                wd.disarm()

For ``imap_unordered`` loops, arm before the first result then re-arm on each
received result to create a per-item heartbeat::

    wd.arm(f"waiting for first result ({n} items in pool)")
    for result in pool.imap_unordered(worker, items):
        wd.arm(f"waiting for next result ({n - done} remaining, last: {result})")
        ...
    wd.disarm()
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional


class Watchdog:
    """Daemon-thread hang detector for blocking NAS I/O.

    Call :meth:`arm` with a description before each potentially-blocking
    operation and :meth:`disarm` immediately after.  If the operation does not
    complete within the configured timeout the watchdog flushes stdout, prints
    a diagnostic to stderr, and calls ``os._exit(1)``.

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
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="Watchdog"
        )
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────

    def arm(self, reason: str, timeout: Optional[float] = None) -> None:
        """Arm the watchdog for an operation described by *reason*.

        If the watchdog is not :meth:`disarm`\\ ed within *timeout* seconds it
        fires.  Re-arming while already armed resets the deadline — call this
        at each iteration of a loop to create a per-item heartbeat.
        """
        deadline = time.monotonic() + (
            timeout if timeout is not None else self._default_timeout
        )
        with self._lock:
            self._reason = reason
            self._deadline = deadline
            self._armed = True

    def disarm(self) -> None:
        """Disarm the watchdog — the guarded operation completed in time."""
        with self._lock:
            self._armed = False

    def stop(self) -> None:
        """Stop the watchdog thread.  Called automatically by the context manager."""
        self._stop.set()
        self._thread.join(timeout=2.0)

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
                f"\nFatal: watchdog timeout ({self._default_timeout:.0f}s) — {reason}",
                file=sys.stderr,
                flush=True,
            )
            self._exit_fn(1)
            return  # only reached when _exit_fn is mocked in tests
