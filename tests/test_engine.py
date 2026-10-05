"""Unit tests for QueueEngine transition detection, dispatching, and fallback cycling."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from agent_queue.client import HerdrClient
from agent_queue.engine import QueueEngine
from agent_queue.state import QueueItem, QueueState


class TestQueueEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")
        self.state = QueueState(self.state_file)
        self.mock_client = MagicMock(spec=HerdrClient)
        self.mock_client.list_machines.return_value = ["notebook"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_startup_seeds_waiting_agents(self):
        engine = QueueEngine(self.state, self.mock_client)

        # On initial tick: p1 is focused, p2 and p3 are idle
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": True, "title": "Focused Task"},
            {"pane_id": "p2", "tab_id": "t2", "agent_status": "idle", "focused": False, "title": "Idle Task 1"},
            {"pane_id": "p3", "tab_id": "t3", "agent_status": "blocked", "focused": False, "title": "Blocked Task 2"},
        ] if m == "Local" else []

        engine.tick()
        # Non-focused idle and blocked agents should be seeded into queue
        self.assertEqual(len(self.state.queue), 2)
        self.assertEqual(self.state.queue[0].pane_id, "p2")
        self.assertEqual(self.state.queue[1].pane_id, "p3")

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
        self.mock_client.focus_agent.assert_called_with("wQ:p2", tab_id="wQ:t2", workspace_id="", machine="notebook")
        self.assertEqual(len(self.state.queue), 0)

    def test_manual_advance_next_and_prev(self):
        engine = QueueEngine(self.state, self.mock_client)
        self.state.push(QueueItem(machine="Local", pane_id="p1", tab_id="t1", title="Task 1"))
        self.state.push(QueueItem(machine="notebook", pane_id="p2", tab_id="t2", title="Task 2"))

        # Manual advance next
        res = engine.advance_next()
        self.assertTrue(res)
        self.mock_client.focus_agent.assert_called_with("p1", tab_id="t1", workspace_id="", machine="Local")

        # Manual advance next again
        res2 = engine.advance_next()
        self.assertTrue(res2)
        self.mock_client.focus_agent.assert_called_with("p2", tab_id="t2", workspace_id="", machine="notebook")

        # Backtrack
        res3 = engine.advance_prev(current_pane_id="p2")
        self.assertTrue(res3)
        self.mock_client.focus_agent.assert_called_with("p1", tab_id="t1", workspace_id="", machine="Local")

    def test_fallback_cycling_when_queue_empty(self):
        engine = QueueEngine(self.state, self.mock_client)
        self.assertEqual(len(self.state.queue), 0)

        # Set up 3 agents on Local: p1 (working, focused), p2 (idle), p3 (idle)
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "workspace_id": "w1", "agent_status": "working", "focused": True, "title": "T1"},
            {"pane_id": "p2", "tab_id": "t2", "workspace_id": "w1", "agent_status": "idle", "focused": False, "title": "T2"},
            {"pane_id": "p3", "tab_id": "t3", "workspace_id": "w1", "agent_status": "idle", "focused": False, "title": "T3"},
        ] if m == "Local" else []

        # Calling advance_next with empty queue should cycle to first waiting agent p2
        res = engine.advance_next(current_pane_id="p1")
        self.assertTrue(res)
        self.mock_client.focus_agent.assert_called_with("p2", tab_id="t2", workspace_id="w1", machine="Local")

        # Calling advance_next from p2 should cycle to p3
        res2 = engine.advance_next(current_pane_id="p2")
        self.assertTrue(res2)
        self.mock_client.focus_agent.assert_called_with("p3", tab_id="t3", workspace_id="w1", machine="Local")

    def test_toggle_auto(self):
        engine = QueueEngine(self.state, self.mock_client)
        val = engine.toggle_auto()
        self.assertTrue(val)
        self.assertTrue(self.state.auto_advance)
        self.mock_client.show_toast.assert_called_with(
            "⚡ Autopilot: ON",
            body="Conveyor mode active: auto-advances to next waiting agent on reply.",
            sound="done",
            position="top-right",
        )
        self.mock_client.show_system_notification.assert_called()

        # Toggle OFF
        val2 = engine.toggle_auto()
        self.assertFalse(val2)
        self.assertFalse(self.state.auto_advance)
        self.mock_client.show_toast.assert_called_with(
            "⏸ Autopilot: OFF",
            body="Manual mode: press Option+n (⌥n) to advance.",
            sound="request",
            position="top-right",
        )

    def test_remote_agents_seeded_when_cache_populates(self):
        engine = QueueEngine(self.state, self.mock_client)

        # Tick 1: Remote cache is still empty
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": True, "title": "Local"}
        ] if m == "Local" else []

        engine.tick()
        self.assertEqual(len(self.state.queue), 0)

        # Tick 2: Background thread populated remote cache with waiting agent in scrape feature
        engine._remote_cache["notebook"] = [
            {"pane_id": "wQ:p2", "tab_id": "wQ:t2", "workspace_id": "wQ", "agent_status": "idle", "focused": False, "title": "Scrape Task"}
        ]

        engine.tick()
        # Newly discovered remote waiting agent should now be seeded!
        self.assertEqual(len(self.state.queue), 1)
        self.assertEqual(self.state.queue[0].pane_id, "wQ:p2")
        self.assertEqual(self.state.queue[0].machine, "notebook")

    def test_show_status(self):
        engine = QueueEngine(self.state, self.mock_client)
        self.state.push(QueueItem(machine="Local", pane_id="p1", title="Task 1"))
        info = engine.show_status()
        self.assertEqual(info["queue_count"], 1)
        self.mock_client.show_toast.assert_called_with(
            "⏸ Autopilot: OFF",
            body="1 waiting agent • Next: Task 1",
            sound="none",
            position="top-right",
        )


if __name__ == "__main__":
    unittest.main()
