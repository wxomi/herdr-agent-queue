"""Unit tests for agent_queue CLI commands and actions."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agent_queue.cli import main


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("agent_queue.cli.QueueEngine")
    def test_cli_next(self, mock_engine_cls):
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.advance_next.return_value = True

        ret = main(["agent_queue", "next"])
        self.assertEqual(ret, 0)
        mock_engine.advance_next.assert_called_once()

    @patch("agent_queue.cli.QueueEngine")
    def test_cli_prev(self, mock_engine_cls):
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.advance_prev.return_value = True

        ret = main(["agent_queue", "prev"])
        self.assertEqual(ret, 0)
        mock_engine.advance_prev.assert_called_once()

    @patch("agent_queue.cli.QueueEngine")
    def test_cli_toggle(self, mock_engine_cls):
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.toggle_auto.return_value = True

        ret = main(["agent_queue", "toggle"])
        self.assertEqual(ret, 0)
        mock_engine.toggle_auto.assert_called_once()


if __name__ == "__main__":
    unittest.main()
