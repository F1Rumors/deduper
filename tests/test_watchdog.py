"""Tests for deduper.watchdog — Watchdog hang detection and PhaseTimer."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.watchdog import PhaseTimer, Watchdog


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


class TestPhaseTimer(unittest.TestCase):

    def test_empty_has_no_outliers(self):
        t = PhaseTimer()
        self.assertEqual(t.outliers(), [])
        self.assertEqual(t.n, 0)

    def test_single_sample_no_outliers(self):
        """stddev is undefined with fewer than two samples — no outliers reported."""
        t = PhaseTimer(threshold_ms=100.0, sigma=3.0)
        t.record(9000.0, "only one")
        self.assertEqual(t.outliers(), [])

    def test_mean_and_stddev(self):
        t = PhaseTimer()
        t.record(100.0, "a")
        t.record(200.0, "b")
        t.record(300.0, "c")
        self.assertAlmostEqual(t.mean_ms, 200.0)
        self.assertAlmostEqual(t.stddev_ms, 100.0, places=5)

    def test_outlier_above_both_sigma_and_threshold(self):
        t = PhaseTimer(threshold_ms=100.0, sigma=3.0)
        for _ in range(100):
            t.record(10.0, "fast")
        t.record(10000.0, "very slow file")
        outliers = t.outliers()
        self.assertEqual(len(outliers), 1)
        self.assertAlmostEqual(outliers[0][0], 10000.0)
        self.assertIn("very slow file", outliers[0][1])

    def test_outlier_below_absolute_threshold_not_reported(self):
        """A statistically extreme value below the floor is not an outlier."""
        t = PhaseTimer(threshold_ms=4000.0, sigma=3.0)
        for _ in range(100):
            t.record(10.0, "fast")
        t.record(500.0, "slightly slow")
        # 500ms is above mean+3σ but below the 4000ms floor
        self.assertEqual(t.outliers(), [])

    def test_top_k_cap(self):
        t = PhaseTimer(threshold_ms=0.0, sigma=0.0, top_k=3)
        for i in range(20):
            t.record(float(i * 10), f"item-{i}")
        outliers = t.outliers()
        self.assertLessEqual(len(outliers), 3)
        self.assertEqual(outliers[0][0], 190.0)

    def test_outliers_sorted_descending(self):
        t = PhaseTimer(threshold_ms=0.0, sigma=0.0, top_k=10)
        t.record(300.0, "b")
        t.record(100.0, "a")
        t.record(200.0, "c")
        outliers = t.outliers()
        ms_vals = [ms for ms, _ in outliers]
        self.assertEqual(ms_vals, sorted(ms_vals, reverse=True))

    def test_multiple_outliers_all_reported(self):
        t = PhaseTimer(threshold_ms=100.0, sigma=3.0, top_k=10)
        for _ in range(50):
            t.record(10.0, "fast")
        t.record(5000.0, "slow-a")
        t.record(6000.0, "slow-b")
        outliers = t.outliers()
        labels = [lbl for _, lbl in outliers]
        self.assertIn("slow-a", labels)
        self.assertIn("slow-b", labels)


class TestWatchdogTimerIntegration(unittest.TestCase):

    def test_arm_disarm_records_timing(self):
        wd = Watchdog(default_timeout=10.0, _exit_fn=lambda _: None)
        wd.arm("operation")
        time.sleep(0.05)
        wd.disarm()
        wd.stop()
        t = wd.timer
        self.assertEqual(t.n, 1)
        self.assertGreater(t.mean_ms, 40.0)   # at least 40ms
        self.assertLess(t.mean_ms, 500.0)     # sanity upper bound

    def test_rearm_records_each_iteration(self):
        wd = Watchdog(default_timeout=10.0, _exit_fn=lambda _: None)
        for _ in range(5):
            wd.arm("item")
            time.sleep(0.02)
        wd.disarm()
        wd.stop()
        # 4 re-arm recordings + 1 disarm recording = 5 total
        self.assertEqual(wd.timer.n, 5)


if __name__ == "__main__":
    unittest.main()
