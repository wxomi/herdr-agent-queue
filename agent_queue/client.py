"""Client wrapper for Herdr CLI and multi-machine inspection."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from agent_queue.config import herdr_bin


def run_cmd(cmd: list[str], timeout: float = 10.0) -> str:
    """Run shell command with timeout and return stdout string."""
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if res.returncode != 0:
            return ""
        return res.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


class HerdrClient:
    """Interacts with Herdr to inspect and focus agents locally and across saved SSH machines."""

    def __init__(self, bin_path: str | None = None):
        self.bin = bin_path or herdr_bin()

    def list_machines(self) -> list[str]:
        """Return list of enabled saved SSH machine labels."""
        out = run_cmd([self.bin, "machine", "list"])
        if not out:
            return []
        machines: list[str] = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 5 and parts[4].strip() == "enabled":
                label = parts[1].strip()
                if label:
                    machines.append(label)
        return machines

    def list_agents(self, machine: str | None = None) -> list[dict[str, Any]]:
        """List active agents on Local machine or remote machine."""
        if machine and machine != "Local":
            cmd = [self.bin, "--machine", machine, "agent", "list"]
        else:
            cmd = [self.bin, "agent", "list"]

        raw = run_cmd(cmd)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            agents = (data.get("result") or {}).get("agents") or []
            return agents
        except json.JSONDecodeError:
            return []

    def focus_agent(
        self,
        pane_id: str,
        tab_id: str | None = None,
        machine: str | None = None,
    ) -> bool:
        """Focus an agent pane and its corresponding tab."""
        base_cmd = [self.bin]
        if machine and machine != "Local":
            base_cmd.extend(["--machine", machine])

        agent_res = run_cmd(base_cmd + ["agent", "focus", pane_id])
        if tab_id:
            run_cmd(base_cmd + ["tab", "focus", tab_id])
        return bool(agent_res)

    def show_toast(
        self,
        title: str,
        body: str | None = None,
        sound: str = "none",
    ) -> None:
        """Display a Herdr toast notification."""
        cmd = [self.bin, "notification", "show", title, "--sound", sound]
        if body:
            cmd.extend(["--body", body])
        run_cmd(cmd)
