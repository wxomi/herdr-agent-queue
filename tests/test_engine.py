"""Unit tests for QueueEngine transition detection and dispatching."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from agent_queue.client import HerdrClient
from agent_queue.engine import QueueEngine
from agent_queue.state import QueueState


class TestQueueEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")
        self.state = QueueState(self.state_file)
        self.mock_client = MagicMock(spec=HerdrClient)
        self.mock_client.list_machines.return_value = ["notebook"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_background_agent_completion_enters_queue(self):
        engine = QueueEngine(self.state, self.mock_client)

        # Tick 1: Agent p1 is working in background
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": False, "title": "Build API"}
        ] if m == "Local" else []

        engine.tick()
        self.assertEqual(len(self.state.queue), 0)

        # Tick 2: Agent p1 finishes and becomes idle
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "idle", "focused": False, "title": "Build API"}
        ] if m == "Local" else []

        engine.tick()
        self.assertEqual(len(self.state.queue), 1)
        self.assertEqual(self.state.queue[0].pane_id, "p1")
        self.assertEqual(self.state.queue[0].machine, "Local")

    def test_auto_advance_triggers_when_focused_agent_starts_working(self):
        self.state.auto_advance = True
        engine = QueueEngine(self.state, self.mock_client)

        # Already have a waiting agent in queue (remote wQ:p2)
        from agent_queue.state import QueueItem
        self.state.push(QueueItem(machine="notebook", pane_id="wQ:p2", tab_id="wQ:t2", title="Aditya Task"))

        # Tick 1: User is focused on local p1, which is idle
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "idle", "focused": True, "title": "Local Task"}
        ] if m == "Local" else []

        engine.tick()

        # Tick 2: User submitted prompt in p1, so p1 becomes working
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": True, "title": "Local Task"}
        ] if m == "Local" else []

        engine.tick()

        # Should have auto-advanced to remote wQ:p2!
        self.mock_client.focus_agent.assert_called_with("wQ:p2", tab_id="wQ:t2", machine="notebook")
        self.assertEqual(len(self.state.queue), 0)

    def test_manual_advance_next_and_prev(self):
        engine = QueueEngine(self.state, self.mock_client)
        from agent_queue.state import QueueItem
        self.state.push(QueueItem(machine="Local", pane_id="p1", tab_id="t1", title="Task 1"))
        self.state.push(QueueItem(machine="notebook", pane_id="p2", tab_id="t2", title="Task 2"))

        # Manual advance next
        res = engine.advance_next()
        self.assertTrue(res)
        self.mock_client.focus_agent.assert_called_with("p1", tab_id="t1", machine="Local")

        # Manual advance next again
        res2 = engine.advance_next()
        self.assertTrue(res2)
        self.mock_client.focus_agent.assert_called_with("p2", tab_id="t2", machine="notebook")

        # Backtrack
        res3 = engine.advance_prev(current_pane_id="p2")
        self.assertTrue(res3)
        self.mock_client.focus_agent.assert_called_with("p1", tab_id="t1", machine="Local")

    def test_toggle_auto(self):
        engine = QueueEngine(self.state, self.mock_client)
        val = engine.toggle_auto()
        self.assertTrue(val)
        self.assertTrue(self.state.auto_advance)
        self.mock_client.show_toast.assert_called()


if __name__ == "__main__":
    unittest.main()
