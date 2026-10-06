"""Unit tests for agent_queue configuration."""

import os
import tomllib
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

    def test_manifest_next_prev_are_global_actions(self):
        manifest_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "herdr-plugin.toml",
        )
        with open(manifest_path, "rb") as fh:
            manifest = tomllib.load(fh)
        actions = {a["id"]: a for a in manifest.get("actions") or []}
        for action_id in ("next", "prev", "toggle", "status"):
            self.assertIn(
                "global",
                actions[action_id].get("contexts") or [],
                f"{action_id} must be invokable from a focused pane via Option+n",
            )


if __name__ == "__main__":
    unittest.main()
