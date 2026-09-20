"""No network or real sleeps: preserve upload time before the runner limit."""
import datetime as dt
import subprocess
import unittest
from unittest.mock import patch
import loop


class RuntimeBudgetTest(unittest.TestCase):
    def test_normal_child_gets_remaining_budget(self):
        with patch.object(loop.time, 'monotonic', return_value=90), patch.object(loop.subprocess, 'call', return_value=0) as call:
            self.assertEqual(loop.bounded_child(['odds_full.py'], {}, 100), 0)
            self.assertEqual(call.call_args.kwargs['timeout'], 10)

    def test_expired_budget_does_not_start_a_child(self):
        with patch.object(loop.time, 'monotonic', return_value=100), patch.object(loop.subprocess, 'call') as call:
            self.assertIsNone(loop.bounded_child(['odds_full.py'], {}, 100))
            call.assert_not_called()

    def test_slow_child_returns_control_for_artifact_upload(self):
        with patch.object(loop.time, 'monotonic', return_value=90), patch.object(loop.subprocess, 'call', side_effect=subprocess.TimeoutExpired('child', 10)):
            self.assertIsNone(loop.bounded_child(['odds_full.py'], {}, 100))

    def test_budget_ends_loop_successfully_before_next_child(self):
        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 15, 12, 0, tzinfo=tz)
        with patch.object(loop.dt, 'datetime', Clock), patch.object(loop.sys, 'argv', ['loop.py', '--until', '2200']), patch.object(loop.time, 'monotonic', side_effect=[0, loop.MAX_RUN_SECONDS]), patch.object(loop.subprocess, 'call') as call:
            self.assertEqual(loop.main(), 0)
            call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
