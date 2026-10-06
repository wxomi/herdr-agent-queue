"""Unit tests for QueueEngine transition detection, dispatching, and fallback cycling."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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
        self.mock_client.list_agents.return_value = []
        self.jump_patch = patch("agent_queue.engine.jump_to_waiting", return_value=False)
        self.back_patch = patch("agent_queue.engine.jump_back", return_value=False)
        self.type_patch = patch("agent_queue.engine.client_typing_fingerprint", return_value="still")
        self.screen_patch = patch("agent_queue.engine.screen_machine", return_value="local")
        self.remember_patch = patch("agent_queue.engine.remember_back")
        self.clear_patch = patch("agent_queue.engine.clear_forward")
        self.expect_patch = patch("agent_queue.engine.take_expected", return_value=False)
        self.mock_jump = self.jump_patch.start()
        self.back_patch.start()
        self.type_patch.start()
        self.screen_patch.start()
        self.mock_remember = self.remember_patch.start()
        self.mock_clear = self.clear_patch.start()
        self.expect_patch.start()

    def tearDown(self):
        self.expect_patch.stop()
        self.clear_patch.stop()
        self.remember_patch.stop()
        self.screen_patch.stop()
        self.type_patch.stop()
        self.back_patch.stop()
        self.jump_patch.stop()
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
        # Only blocked/done count as waiting; idle means the user already saw it
        self.assertEqual([q.pane_id for q in self.state.queue], ["p3"])

    def test_queued_agent_dropped_once_seen(self):
        engine = QueueEngine(self.state, self.mock_client)
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "done", "focused": False, "title": "Done"},
        ] if m == "Local" else []
        engine.tick()
        self.assertEqual(len(self.state.queue), 1)

        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "idle", "focused": False, "title": "Done"},
        ] if m == "Local" else []
        engine.tick()
        self.assertEqual(len(self.state.queue), 0)

    def test_next_uses_native_jump_when_something_waits(self):
        self.mock_jump.return_value = True
        engine = QueueEngine(self.state, self.mock_client)
        self.state.push(QueueItem(machine="notebook", pane_id="wQ:pC", title="Scrape"))
        self.assertTrue(engine.advance_next())
        self.mock_jump.assert_called_once_with(self.mock_client)
        self.mock_client.prefocus_remote_async.assert_not_called()
        self.mock_client.focus_agent.assert_not_called()

    def test_remote_working_to_idle_is_queued(self):
        engine = QueueEngine(self.state, self.mock_client)
        local = [{"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": True, "title": "Me"}]
        engine._remote_cache["notebook"] = [
            {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "working", "focused": True, "title": "Scrape"}
        ]
        self.mock_client.list_agents.side_effect = lambda m: local if m == "Local" else engine._remote_cache["notebook"]
        engine.tick()
        self.assertEqual(len(self.state.queue), 0)

        # Remote pane is "focused" on its own server, but the user is on Local: still queue it
        engine._remote_cache["notebook"] = [
            {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "done", "focused": True, "title": "Scrape"}
        ]
        engine.tick()
        self.assertEqual([(q.machine, q.pane_id) for q in self.state.queue], [("notebook", "wQ:pC")])

    def test_background_agent_completion_enters_queue(self):
        engine = QueueEngine(self.state, self.mock_client)

        # Tick 1: Agent p1 is working in background
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": False, "title": "Build API"}
        ] if m == "Local" else []

        engine.tick()
        self.assertEqual(len(self.state.queue), 0)

        # Tick 2: Agent p1 finishes unseen
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "done", "focused": False, "title": "Build API"}
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

        remote = [{"pane_id": "wQ:p2", "tab_id": "wQ:t2", "agent_status": "done", "focused": False, "title": "Aditya Task"}]

        # Tick 1: User is focused on local p1, which is idle
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "idle", "focused": True, "title": "Local Task"}
        ] if m == "Local" else remote

        engine.tick()

        # Tick 2: User submitted prompt in p1, so p1 becomes working
        self.mock_client.list_agents.side_effect = lambda m: [
            {"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": True, "title": "Local Task"}
        ] if m == "Local" else remote

        engine.tick()

        # Remote target is pre-focused in the background, never via blocking SSH focus
        self.mock_client.prefocus_remote_async.assert_called_with("notebook", "wQ:p2", tab_id="wQ:t2", workspace_id="")
        self.mock_client.focus_agent.assert_not_called()
        self.assertEqual(len(self.state.queue), 0)

    def test_autopilot_opens_the_agent_that_finishes_while_you_wait(self):
        self.state.auto_advance = True
        engine = QueueEngine(self.state, self.mock_client)
        parked = [{"pane_id": "p1", "tab_id": "t1", "agent_status": "idle", "focused": True, "title": "Parked"}]
        self.mock_client.list_agents.side_effect = lambda m: parked if m == "Local" else [
            {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "working", "focused": False, "title": "Scrape"}
        ]
        engine._remote_cache["notebook"] = [
            {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "working", "focused": False, "title": "Scrape"}
        ]
        with patch("agent_queue.engine.screen_machine", return_value="local"), \
             patch("agent_queue.engine.open_finished", return_value=True) as opened:
            engine.tick()
            engine._typed_at = 0
            engine._remote_cache["notebook"] = [
                {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "idle", "focused": False, "title": "Scrape"}
            ]
            self.mock_client.list_agents.side_effect = lambda m: parked if m == "Local" else engine._remote_cache["notebook"]
            engine.tick()
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[1], "notebook")
        self.assertEqual(opened.call_args.args[2]["pane_id"], "wQ:pC")

    def test_autopilot_waits_while_you_are_typing(self):
        self.state.auto_advance = True
        engine = QueueEngine(self.state, self.mock_client)
        parked = [{"pane_id": "p1", "tab_id": "t1", "agent_status": "idle", "focused": True, "title": "Parked"}]
        engine._remote_cache["notebook"] = [
            {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "working", "focused": False, "title": "Scrape"}
        ]
        self.mock_client.list_agents.side_effect = lambda m: parked if m == "Local" else engine._remote_cache["notebook"]
        with patch("agent_queue.engine.screen_machine", return_value="local"), \
             patch("agent_queue.engine.client_typing_fingerprint", side_effect=["a", "ab", "ab"]), \
             patch("agent_queue.engine.open_finished", return_value=True) as opened:
            engine.tick()
            engine._remote_cache["notebook"] = [
                {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "idle", "focused": False, "title": "Scrape"}
            ]
            engine.tick()
            self.assertIsNotNone(engine._pending_finish)
            opened.assert_not_called()
            engine._typed_at = 0
            engine.tick()
        opened.assert_called_once()

    def test_autopilot_stays_when_you_are_watching_a_working_agent(self):
        self.state.auto_advance = True
        engine = QueueEngine(self.state, self.mock_client)
        watching = [{"pane_id": "p1", "tab_id": "t1", "agent_status": "working", "focused": True, "title": "Busy"}]
        engine._remote_cache["notebook"] = [
            {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "working", "focused": False, "title": "Scrape"}
        ]
        self.mock_client.list_agents.side_effect = lambda m: watching if m == "Local" else engine._remote_cache["notebook"]
        with patch("agent_queue.engine.screen_machine", return_value="local"), \
             patch("agent_queue.engine.open_finished", return_value=True) as opened:
            engine.tick()
            engine._typed_at = 0
            engine._remote_cache["notebook"] = [
                {"pane_id": "wQ:pC", "tab_id": "wQ:tB", "workspace_id": "wQ", "agent_status": "idle", "focused": False, "title": "Scrape"}
            ]
            engine.tick()
        opened.assert_not_called()

    def test_manual_switch_is_where_option_p_returns(self):
        engine = QueueEngine(self.state, self.mock_client)
        first = [{"pane_id": "p1", "tab_id": "t1", "workspace_id": "wE", "agent_status": "idle", "focused": True, "title": "First"}]
        second = [
            {"pane_id": "p1", "tab_id": "t1", "workspace_id": "wE", "agent_status": "idle", "focused": False, "title": "First"},
            {"pane_id": "p2", "tab_id": "t2", "workspace_id": "wE", "agent_status": "idle", "focused": True, "title": "Second"},
        ]
        self.mock_client.list_agents.side_effect = lambda m: first if m == "Local" else []
        engine.tick()
        self.mock_remember.assert_not_called()
        self.mock_client.list_agents.side_effect = lambda m: second if m == "Local" else []
        engine.tick()
        self.mock_remember.assert_called_once()
        self.assertEqual(self.mock_remember.call_args.args[0]["pane_id"], "p1")
        self.assertEqual(self.mock_remember.call_args.args[0]["title"], "First")
        self.mock_clear.assert_called_once()

    def test_manual_advance_next_and_prev(self):
        engine = QueueEngine(self.state, self.mock_client)
        self.state.push(QueueItem(machine="notebook", pane_id="p2", tab_id="t2", title="Task 2"))
        self.state.push(QueueItem(machine="Local", pane_id="p1", tab_id="t1", title="Task 1"))
        self.mock_client.list_agents.return_value = [{"pane_id": "p1", "agent_status": "done", "focused": False}]

        # Oldest waiting first, regardless of machine; remote is pre-focused in background
        res = engine.advance_next()
        self.assertTrue(res)
        self.mock_client.prefocus_remote_async.assert_called_with("notebook", "p2", tab_id="t2", workspace_id="")
        self.mock_client.focus_agent.assert_not_called()

        res2 = engine.advance_next()
        self.assertTrue(res2)
        self.mock_client.focus_agent.assert_called_with("p1", tab_id="t1", workspace_id="", machine="Local")

        # Backtrack from p1 to p2
        res3 = engine.advance_prev(current_pane_id="p1")
        self.assertTrue(res3)
        self.assertEqual(self.mock_client.prefocus_remote_async.call_count, 2)

    def test_empty_queue_does_not_jump_to_idle_agents(self):
        engine = QueueEngine(self.state, self.mock_client)
        res = engine.advance_next(current_pane_id="p1")
        self.assertFalse(res)
        self.mock_client.focus_agent.assert_not_called()
        self.mock_client.prefocus_remote_async.assert_not_called()
        self.mock_client.list_machines.assert_not_called()

    def test_toggle_auto(self):
        engine = QueueEngine(self.state, self.mock_client)
        val = engine.toggle_auto()
        self.assertTrue(val)
        self.assertTrue(self.state.auto_advance)
        self.mock_client.show_toast.assert_called_with(
            "⚡ Autopilot: ON",
            body="Jumps when you send a reply, and when another agent finishes while you sit in an idle session.",
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
            {"pane_id": "wQ:p2", "tab_id": "wQ:t2", "workspace_id": "wQ", "agent_status": "done", "focused": False, "title": "Scrape Task"}
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
