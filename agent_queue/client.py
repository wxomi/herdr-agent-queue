"""Client wrapper for Herdr Unix domain socket IPC and multi-machine inspection."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from agent_queue.config import SOCKET_PATH, herdr_bin


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
        return res.stdout or "ok"
    except (OSError, subprocess.TimeoutExpired):
        return ""


class HerdrClient:
    """Interacts with Herdr via direct Unix domain socket IPC (<1ms) or CLI fallback."""

    def __init__(self, bin_path: str | None = None, sock_path: str | None = SOCKET_PATH):
        self.bin = bin_path or herdr_bin()
        self.sock_path = sock_path
        self._machines_cache: list[str] | None = None
        self._machines_cache_time: float = 0.0

    def is_socket_available(self) -> bool:
        """Check if local Herdr Unix domain socket exists."""
        return bool(self.sock_path and os.path.exists(self.sock_path))

    def server_identity(self) -> tuple[int, int] | None:
        """Return (dev, ino) of the Herdr socket file, or None if unavailable."""
        if not self.sock_path:
            return None
        try:
            st = os.stat(self.sock_path)
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    def call_socket(self, method: str, params: dict | None = None, req_id: str = "q") -> dict:
        """Call Herdr JSON-RPC directly over Unix domain socket with sub-millisecond latency."""
        if not self.is_socket_available():
            return {}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(self.sock_path)
                payload = json.dumps({"id": req_id, "method": method, "params": params or {}}) + "\n"
                s.sendall(payload.encode("utf-8"))
                buf = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n" in chunk:
                        break
                if not buf:
                    return {}
                return json.loads(buf.decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def list_machines(self, force_refresh: bool = False, cache_ttl: float = 30.0) -> list[str]:
        """Return list of enabled saved SSH machine labels (cached for cache_ttl seconds)."""
        now = time.monotonic()
        if not force_refresh and self._machines_cache is not None and (now - self._machines_cache_time < cache_ttl):
            return self._machines_cache

        out = run_cmd([self.bin, "machine", "list"], timeout=3.0)
        if not out:
            return self._machines_cache if self._machines_cache is not None else []
        machines: list[str] = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 5 and parts[4].strip() == "enabled":
                label = parts[1].strip()
                if label:
                    machines.append(label)
        self._machines_cache = machines
        self._machines_cache_time = now
        return machines

    def list_agents(self, machine: str | None = None) -> list[dict[str, Any]]:
        """List active agents on Local machine (via socket <0.3ms) or remote machine (via CLI)."""
        if machine and machine != "Local":
            cmd = [self.bin, "--machine", machine, "agent", "list"]
            raw = run_cmd(cmd, timeout=3.0)
            if not raw:
                return []
            try:
                data = json.loads(raw)
                return (data.get("result") or {}).get("agents") or []
            except json.JSONDecodeError:
                return []

        # Local machine: use direct Unix domain socket (<0.3ms)
        if self.is_socket_available():
            res = self.call_socket("agent.list")
            agents = (res.get("result") or {}).get("agents")
            if agents is not None:
                return agents

        # Fallback to CLI if socket unavailable
        raw = run_cmd([self.bin, "agent", "list"])
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return (data.get("result") or {}).get("agents") or []
        except json.JSONDecodeError:
            return []

    def focus_agent(
        self,
        pane_id: str,
        tab_id: str | None = None,
        workspace_id: str | None = None,
        machine: str | None = None,
    ) -> bool:
        """Focus an agent pane, its tab, and optionally its workspace."""
        if machine and machine != "Local":
            base_cmd = [self.bin, "--machine", machine]
            if workspace_id:
                run_cmd(base_cmd + ["workspace", "focus", workspace_id])
            agent_res = run_cmd(base_cmd + ["agent", "focus", pane_id])
            if tab_id:
                run_cmd(base_cmd + ["tab", "focus", tab_id])
            return bool(agent_res)

        # Local machine: use direct Unix domain socket (<1ms total)
        if self.is_socket_available():
            if workspace_id:
                self.call_socket("workspace.focus", {"workspace_id": workspace_id})
            res = self.call_socket("agent.focus", {"target": pane_id})
            if tab_id:
                self.call_socket("tab.focus", {"tab_id": tab_id})
            if res and "result" in res:
                return True

        # Fallback to CLI
        base_cmd = [self.bin]
        if workspace_id:
            run_cmd(base_cmd + ["workspace", "focus", workspace_id])
        agent_res = run_cmd(base_cmd + ["agent", "focus", pane_id])
        if tab_id:
            run_cmd(base_cmd + ["tab", "focus", tab_id])
        return bool(agent_res)

    def show_toast(
        self,
        title: str,
        body: str | None = None,
        sound: str = "none",
        position: str = "top-right",
    ) -> None:
        """Display a Herdr toast notification (<0.1ms over socket)."""
        if self.is_socket_available():
            params: dict[str, Any] = {"title": title, "sound": sound, "position": position}
            if body:
                params["body"] = body
            res = self.call_socket("notification.show", params)
            if res and "result" in res:
                return

        cmd = [self.bin, "notification", "show", title, "--sound", sound, "--position", position]
        if body:
            cmd.extend(["--body", body])
        run_cmd(cmd)

    def show_system_notification(
        self,
        title: str,
        message: str,
        subtitle: str | None = None,
    ) -> None:
        """Display an OS-native desktop notification (macOS or Linux)."""
        if sys.platform == "darwin":
            sub_part = f' subtitle "{subtitle}"' if subtitle else ""
            msg_clean = message.replace('"', '\\"')
            title_clean = title.replace('"', '\\"')
            script = f'display notification "{msg_clean}" with title "{title_clean}"{sub_part}'
            try:
                subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif sys.platform.startswith("linux"):
            summary = f"{title}: {subtitle}" if subtitle else title
            try:
                subprocess.run(
                    ["notify-send", summary, message],
                    capture_output=True,
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
