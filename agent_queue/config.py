"""Configuration paths and defaults for herdr-agent-queue."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Paths
STATE_DIR = os.environ.get(
    "HERDR_AGENT_QUEUE_STATE_DIR",
    os.path.expanduser("~/.local/state/herdr/plugins/wxomi.agent-queue"),
)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOCK_FILE = os.path.join(STATE_DIR, "daemon.lock")
SOCKET_PATH = os.environ.get(
    "HERDR_SOCKET_PATH",
    os.path.expanduser("~/.config/herdr/herdr.sock"),
)

# Timing & Defaults
DEFAULT_AUTO_ADVANCE = False  # Manual reflex by default
POLL_INTERVAL = float(os.environ.get("HERDR_AGENT_QUEUE_INTERVAL", "0.5"))
IDLE_POLL_INTERVAL = float(os.environ.get("HERDR_AGENT_QUEUE_IDLE_INTERVAL", "1.5"))


def herdr_bin() -> str:
    """Find herdr binary path with in-place update fallback."""
    bin_path = os.environ.get("HERDR_BIN_PATH", "").removesuffix(" (deleted)")
    if bin_path and Path(bin_path).is_file():
        return bin_path
    return shutil.which("herdr") or str(Path.home() / ".local/bin/herdr")
