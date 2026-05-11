"""Tests for deduper.watchdog — Watchdog hang detection."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.watchdog import Watchdog


class TestWatchdogDoesNotFire(unittest.TestCase):

    def test_unarmed_does_not_fire(self):
        fired = threading.Event()
        with Watchdog(default_timeout=0.1, _exit_fn=lambda _: fired.set()):
            time.sleep(0.4)
        self.assertFalse(fired.is_set())

    def test_disarmed_before_timeout_does_not_fire(self):
        fired = threading.Event()
        with Watchdog(default_timeout=0.3, _exit_fn=lambda _: fired.set()) as wd:
            wd.arm("test operation")
            wd.disarm()
            time.sleep(0.5)
        self.assertFalse(fired.is_set())

    def test_rearm_extends_deadline(self):
        """Re-arming each iteration prevents firing even across many iterations."""
        fired = threading.Event()
        with Watchdog(default_timeout=0.3, _exit_fn=lambda _: fired.set()) as wd:
            for _ in range(4):
                wd.arm("still working")
                time.sleep(0.15)  # 0.15 < 0.3, so each re-arm resets the clock
            wd.disarm()
        self.assertFalse(fired.is_set())

    def test_context_manager_stops_thread_before_fire(self):
        """Exiting the context manager must stop the watchdog before it fires."""
        fired = threading.Event()
        with Watchdog(default_timeout=0.5, _exit_fn=lambda _: fired.set()) as wd:
            wd.arm("operation")
            # exit context well before the 0.5s deadline
        time.sleep(0.7)
        self.assertFalse(fired.is_set())


class TestWatchdogFires(unittest.TestCase):

    def test_fires_after_timeout(self):
        fired = threading.Event()
        wd = Watchdog(default_timeout=0.1, _exit_fn=lambda _: fired.set())
        wd.arm("blocking on /nas/stuck.jpg")
        self.assertTrue(fired.wait(timeout=2.0), "Watchdog did not fire within 2s")
        wd.stop()

    def test_fires_exactly_once(self):
        """After firing, the watchdog thread exits — _exit_fn is called once only."""
        call_count = [0]
        wd = Watchdog(default_timeout=0.1, _exit_fn=lambda _: call_count.__setitem__(0, call_count[0] + 1))
        wd.arm("stuck")
        time.sleep(0.6)
        wd.stop()
        self.assertEqual(call_count[0], 1)

    def test_exit_called_with_code_1(self):
        exit_codes = []
        wd = Watchdog(default_timeout=0.1, _exit_fn=exit_codes.append)
        wd.arm("stuck")
        deadline = time.monotonic() + 2.0
        while not exit_codes and time.monotonic() < deadline:
            time.sleep(0.05)
        wd.stop()
        self.assertEqual(exit_codes, [1])

    def test_message_contains_reason(self):
        """The reason string passed to arm() must appear in the fatal message."""
        import io
        fired = threading.Event()
        buf = io.StringIO()

        def fake_exit(_):
            fired.set()

        wd = Watchdog(default_timeout=0.1, _exit_fn=fake_exit)
        original_stderr = sys.stderr
        sys.stderr = buf
        try:
            wd.arm("hashing /volume1/shared/photos/stuck_file.jpg")
            fired.wait(timeout=2.0)
        finally:
            sys.stderr = original_stderr
        wd.stop()

        self.assertIn("stuck_file.jpg", buf.getvalue())


class TestWatchdogStop(unittest.TestCase):

    def test_stop_is_idempotent(self):
        wd = Watchdog(default_timeout=1.0, _exit_fn=lambda _: None)
        wd.stop()
        wd.stop()  # should not raise

    def test_stop_joins_thread(self):
        wd = Watchdog(default_timeout=1.0, _exit_fn=lambda _: None)
        wd.stop()
        self.assertFalse(wd._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
