"""Unit tests for Herdr client and multi-machine discovery."""

import json
import unittest
from unittest.mock import MagicMock, patch

from agent_queue.client import HerdrClient


class TestHerdrClient(unittest.TestCase):
    @patch("agent_queue.client.run_cmd")
    def test_list_machines(self, mock_run):
        mock_run.return_value = (
            "16eb716d\tnotebook\twxomi@100.101.200.2\tdefault\tenabled\n"
            "abc12345\totherbox\tuser@host\tdefault\tdisabled\n"
        )
        client = HerdrClient(bin_path="herdr")
        machines = client.list_machines()
        self.assertEqual(machines, ["notebook"])

    @patch("agent_queue.client.run_cmd")
    def test_list_agents_local(self, mock_run):
        sample_response = {
            "result": {
                "agents": [
                    {
                        "agent": "agy",
                        "pane_id": "p1",
                        "tab_id": "t1",
                        "workspace_id": "w1",
                        "agent_status": "idle",
                        "focused": True,
                        "title": "Local Task",
                    }
                ]
            }
        }
        mock_run.return_value = json.dumps(sample_response)
        client = HerdrClient(bin_path="herdr")
        agents = client.list_agents(machine="Local")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["pane_id"], "p1")
        mock_run.assert_called_with(["herdr", "agent", "list"])

    @patch("agent_queue.client.run_cmd")
    def test_list_agents_remote(self, mock_run):
        sample_response = {
            "result": {
                "agents": [
                    {
                        "agent": "kiro",
                        "pane_id": "wQ:p2",
                        "tab_id": "wQ:t2",
                        "workspace_id": "wQ",
                        "agent_status": "working",
                        "focused": False,
                        "title": "Remote Task",
                    }
                ]
            }
        }
        mock_run.return_value = json.dumps(sample_response)
        client = HerdrClient(bin_path="herdr")
        agents = client.list_agents(machine="notebook")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["pane_id"], "wQ:p2")
        mock_run.assert_called_with(["herdr", "--machine", "notebook", "agent", "list"])

    @patch("agent_queue.client.run_cmd")
    def test_focus_agent_remote(self, mock_run):
        mock_run.return_value = '{"id":"ok"}'
        client = HerdrClient()
        success = client.focus_agent("wQ:p2", tab_id="wQ:t2", machine="notebook")
        self.assertTrue(success)
        # Should call agent focus, then tab focus
        self.assertEqual(mock_run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
