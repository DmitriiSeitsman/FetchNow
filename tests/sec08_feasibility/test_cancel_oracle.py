"""Local regressions for the cancellation oracle. No /proc and no root."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cancel_oracle as oracle

STAT_SLEEP = "42 (sec08 sleep) S 7 42 99 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 12345 0\n"
STAT_ZOMBIE = "42 (sec08 sleep) Z 7 42 99 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 12345 0\n"
STAT_REUSED = "42 (other) S 1 9 9 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 99999 0\n"
STAT_STOPPED = "42 (sec08 sleep) T 7 42 99 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 12345 0\n"


class CancelOracleTest(unittest.TestCase):
    def test_live_sleeping_process_is_not_termination(self) -> None:
        item = oracle.classify_pid(STAT_SLEEP, expected_starttime=12345)
        self.assertEqual(item["class"], "sleeping")
        self.assertEqual(item["ppid"], 7)
        self.assertEqual(item["pgrp"], 42)
        self.assertEqual(item["session"], 99)
        self.assertFalse(oracle.termination_proven([item]))
        self.assertFalse(oracle.reaping_proven(set(), {42}))

    def test_running_process_is_not_termination(self) -> None:
        text = STAT_SLEEP.replace(" S ", " R ", 1)
        item = oracle.classify_pid(text, expected_starttime=12345)
        self.assertEqual(item["class"], "running")
        self.assertFalse(oracle.termination_proven([item]))

    def test_exited_unreaped_child_is_zombie_not_reaped(self) -> None:
        item = oracle.classify_pid(STAT_ZOMBIE, expected_starttime=12345)
        self.assertEqual(item["class"], "zombie")
        self.assertTrue(oracle.termination_proven([item]))
        self.assertFalse(oracle.reaping_proven(set(), {42}))

    def test_reaped_child_is_absent(self) -> None:
        item = oracle.classify_pid(None, expected_starttime=12345, read_error="absent")
        self.assertEqual(item["class"], "reaped")
        self.assertTrue(oracle.termination_proven([item]))
        self.assertTrue(oracle.reaping_proven({42}, {42}))

    def test_pid_reuse_is_not_success(self) -> None:
        item = oracle.classify_pid(STAT_REUSED, expected_starttime=12345)
        self.assertEqual(item["class"], "pid_reused")
        self.assertEqual(item["starttime"], 99999)
        self.assertNotIn(item["class"], oracle.TERMINATED_CLASSES)
        self.assertFalse(oracle.termination_proven([item]))

    def test_observation_error_is_not_success(self) -> None:
        item = oracle.classify_pid(None, expected_starttime=12345, read_error="EIO")
        self.assertEqual(item["class"], "observation_error")
        self.assertFalse(oracle.termination_proven([item]))
        unparsed = oracle.classify_pid("not a stat", expected_starttime=None)
        self.assertEqual(unparsed["class"], "observation_error")
        self.assertFalse(oracle.termination_proven([unparsed]))

    def test_stopped_process_is_not_terminated(self) -> None:
        item = oracle.classify_pid(STAT_STOPPED, expected_starttime=12345)
        self.assertEqual(item["class"], "stopped")
        self.assertFalse(oracle.termination_proven([item]))

    def test_empty_set_does_not_pass(self) -> None:
        self.assertFalse(oracle.targets_were_running([]))
        self.assertFalse(oracle.termination_proven([]))
        self.assertFalse(oracle.reaping_proven(set(), set()))

    def test_partial_reap_is_not_reaping_pass(self) -> None:
        self.assertFalse(oracle.reaping_proven({42}, {42, 43}))

    def test_preimage_requires_every_target_running(self) -> None:
        live = oracle.classify_pid(STAT_SLEEP, expected_starttime=12345)
        dead = oracle.classify_pid(STAT_ZOMBIE, expected_starttime=12345)
        self.assertTrue(oracle.targets_were_running([live, live]))
        self.assertFalse(oracle.targets_were_running([live, dead]))


if __name__ == "__main__":
    unittest.main()
