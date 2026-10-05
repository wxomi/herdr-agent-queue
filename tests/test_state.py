"""Unit tests for QueueState persistence and operations."""

import os
import tempfile
import unittest

from agent_queue.state import QueueItem, QueueState


class TestQueueState(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_state(self):
        qs = QueueState(self.state_file)
        self.assertFalse(qs.auto_advance)
        self.assertEqual(len(qs.queue), 0)
        self.assertEqual(len(qs.history), 0)

    def test_push_and_pop_next(self):
        qs = QueueState(self.state_file)
        item1 = QueueItem(machine="Local", pane_id="p1", tab_id="t1", title="Task 1")
        item2 = QueueItem(machine="notebook", pane_id="p2", tab_id="t2", title="Task 2")

        self.assertTrue(qs.push(item1))
        self.assertTrue(qs.push(item2))
        # Duplicate push should return False and not duplicate
        self.assertFalse(qs.push(item1))
        self.assertEqual(len(qs.queue), 2)

        next_item = qs.pop_next()
        self.assertEqual(next_item.pane_id, "p1")
        self.assertEqual(len(qs.queue), 1)
        self.assertEqual(len(qs.history), 1)
        self.assertEqual(qs.history[-1].pane_id, "p1")

    def test_pop_prev_backtracking(self):
        qs = QueueState(self.state_file)
        item1 = QueueItem(machine="Local", pane_id="p1", tab_id="t1", title="Task 1")
        item2 = QueueItem(machine="Local", pane_id="p2", tab_id="t2", title="Task 2")

        qs.push(item1)
        qs.push(item2)
        qs.pop_next()  # Focuses p1
        qs.pop_next()  # Focuses p2

        # Currently at p2; pop_prev should take us back to p1
        prev_item = qs.pop_prev(current_pane_id="p2")
        self.assertIsNotNone(prev_item)
        self.assertEqual(prev_item.pane_id, "p1")

    def test_toggle_auto_advance(self):
        qs = QueueState(self.state_file)
        self.assertFalse(qs.auto_advance)
        val = qs.toggle_auto_advance()
        self.assertTrue(val)
        self.assertTrue(qs.auto_advance)

        # Reload from disk
        qs2 = QueueState(self.state_file)
        self.assertTrue(qs2.auto_advance)

    def test_remove_pane(self):
        qs = QueueState(self.state_file)
        item = QueueItem(machine="Local", pane_id="p1", tab_id="t1", title="Task 1")
        qs.push(item)
        self.assertEqual(len(qs.queue), 1)
        qs.remove("p1")
        self.assertEqual(len(qs.queue), 0)


if __name__ == "__main__":
    unittest.main()
