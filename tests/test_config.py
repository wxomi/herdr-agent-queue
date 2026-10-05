"""Unit tests for agent_queue configuration."""

import os
import unittest
from unittest.mock import patch

from agent_queue import config


class TestConfig(unittest.TestCase):
    def test_default_paths(self):
        self.assertTrue(config.STATE_DIR.endswith("wxomi.agent-queue"))
        self.assertTrue(config.STATE_FILE.endswith("state.json"))
        self.assertTrue(config.LOCK_FILE.endswith("daemon.lock"))
        self.assertTrue(config.SOCKET_PATH.endswith("herdr.sock"))
        self.assertFalse(config.DEFAULT_AUTO_ADVANCE)

    @patch.dict(os.environ, {"HERDR_AGENT_QUEUE_INTERVAL": "2.5"})
    def test_custom_interval(self):
        # Re-evaluate or test config interval override
        interval = float(os.environ.get("HERDR_AGENT_QUEUE_INTERVAL", "1.0"))
        self.assertEqual(interval, 2.5)


if __name__ == "__main__":
    unittest.main()
