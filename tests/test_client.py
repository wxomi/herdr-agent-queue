"""Unit tests for Herdr client, direct socket IPC, and multi-machine discovery."""

import json
import unittest
from unittest.mock import MagicMock, patch

from agent_queue.client import HerdrClient


class TestHerdrClient(unittest.TestCase):
    @patch("agent_queue.client.run_cmd")
    def test_list_machines_and_caching(self, mock_run):
        mock_run.return_value = (
            "16eb716d\tnotebook\twxomi@100.101.200.2\tdefault\tenabled\n"
            "abc12345\totherbox\tuser@host\tdefault\tdisabled\n"
        )
        client = HerdrClient(bin_path="herdr", sock_path=None)
        machines = client.list_machines(force_refresh=True)
        self.assertEqual(machines, ["notebook"])
        self.assertEqual(mock_run.call_count, 1)

        # Second call within TTL should return cached list without invoking run_cmd
        cached = client.list_machines()
        self.assertEqual(cached, ["notebook"])
        self.assertEqual(mock_run.call_count, 1)

    @patch("agent_queue.client.run_cmd")
    def test_list_agents_local_cli_fallback(self, mock_run):
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
        client = HerdrClient(bin_path="herdr", sock_path=None)
        agents = client.list_agents(machine="Local")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["pane_id"], "p1")
        mock_run.assert_called_with(["herdr", "agent", "list"])

    def test_list_agents_local_socket(self):
        client = HerdrClient(bin_path="herdr", sock_path="/tmp/fake.sock")
        with patch.object(client, "is_socket_available", return_value=True), \
             patch.object(client, "call_socket") as mock_socket:
            mock_socket.return_value = {
                "result": {
                    "agents": [
                        {
                            "agent": "agy",
                            "pane_id": "pSocket",
                            "agent_status": "idle",
                        }
                    ]
                }
            }
            agents = client.list_agents(machine="Local")
            self.assertEqual(len(agents), 1)
            self.assertEqual(agents[0]["pane_id"], "pSocket")
            mock_socket.assert_called_once_with("agent.list")

    def test_focus_agent_socket(self):
        client = HerdrClient(bin_path="herdr", sock_path="/tmp/fake.sock")
        with patch.object(client, "is_socket_available", return_value=True), \
             patch.object(client, "call_socket") as mock_socket:
            mock_socket.return_value = {"result": {"ok": True}}
            res = client.focus_agent("p1", tab_id="t1", workspace_id="w1", machine="Local")
            self.assertTrue(res)
            self.assertEqual(mock_socket.call_count, 3)

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
        client = HerdrClient(bin_path="herdr", sock_path=None)
        with patch("agent_queue.native_jump.remote_agents", return_value=None):
            agents = client.list_agents(machine="notebook")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["pane_id"], "wQ:p2")
        mock_run.assert_called_with(["herdr", "--machine", "notebook", "agent", "list"], timeout=3.0)

    @patch("agent_queue.client.run_cmd")
    def test_focus_agent_remote(self, mock_run):
        mock_run.return_value = '{"id":"ok"}'
        client = HerdrClient(sock_path=None)
        success = client.focus_agent("wQ:p2", tab_id="wQ:t2", machine="notebook")
        self.assertTrue(success)
        self.assertEqual(mock_run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
