"""Unit tests for native_jump key encoding."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from agent_queue.native_jump import (
    _key_bytes,
    _lookup_wezterm_pane,
    _ssh,
    dispatch_remote,
    ensure_back_bridge,
    header_machine,
    jump_back,
    jump_to_waiting,
    next_agent_sequence,
    pick_next_waiter,
    push_back,
    sidebar_text,
)


class TestNativeJump(unittest.TestCase):
    def setUp(self):
        fd, self.forward_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        fd, self.expect_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._fwd = patch("agent_queue.native_jump.FORWARD_FILE", self.forward_path)
        self._exp = patch("agent_queue.native_jump.EXPECT_FILE", self.expect_path)
        self._fwd.start()
        self._exp.start()

    def tearDown(self):
        self._exp.stop()
        self._fwd.stop()
        for path in (self.forward_path, self.expect_path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _cfg(self, body: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w") as f:
            f.write(body)
        self.addCleanup(os.remove, path)
        return path

    def test_key_bytes(self):
        self.assertEqual(_key_bytes("ctrl+b"), b"\x02")
        self.assertEqual(_key_bytes("]"), b"]")
        self.assertEqual(_key_bytes("alt+n"), b"\x1bn")
        self.assertIsNone(_key_bytes("shift+tab"))

    def test_prefix_binding_uses_default_prefix(self):
        path = self._cfg('[keys]\nnext_agent = "prefix+]"\n')
        self.assertEqual(next_agent_sequence(path), [b"\x02", b"]"])

    def test_custom_prefix(self):
        path = self._cfg('[keys]\nprefix = "ctrl+a"\nnext_agent = "prefix+]"\n')
        self.assertEqual(next_agent_sequence(path), [b"\x01", b"]"])

    def test_unbound_next_agent(self):
        path = self._cfg("[keys]\n")
        self.assertIsNone(next_agent_sequence(path))

    def test_jump_uses_local_socket_and_stops_when_focus_moves(self):
        client = MagicMock()
        client.call_socket.side_effect = [
            {"result": {"snapshot": {"focused_pane_id": "p1", "focused_workspace_id": "w1"}}},
            {"result": {"snapshot": {"focused_pane_id": "p2", "focused_workspace_id": "w9"}}},
        ]
        with patch("agent_queue.native_jump.sys.platform", "darwin"), \
             patch("agent_queue.native_jump._wezterm", return_value="/wezterm"), \
             patch("agent_queue.native_jump.next_agent_sequence", return_value=[b"\x02", b"]"]), \
             patch("agent_queue.native_jump.find_wezterm_pane", return_value=7), \
             patch("agent_queue.native_jump.focus_sibling", return_value=False), \
             patch("agent_queue.native_jump._focused_agent", return_value=None), \
             patch("agent_queue.native_jump._screen", return_value=""), \
             patch("agent_queue.native_jump.set_waiting_view", return_value=True) as set_view, \
             patch("agent_queue.native_jump._send", return_value=True) as send, \
             patch("agent_queue.native_jump.restore_this_space") as restore, \
             patch("agent_queue.native_jump.subprocess.run") as run, \
             patch("agent_queue.native_jump.time.sleep"):
            self.assertTrue(jump_to_waiting(client))
        set_view.assert_called_once_with(client)
        self.assertEqual(send.call_count, 2)
        restore.assert_called_once_with(client)
        run.assert_not_called()

    def test_pick_skips_the_open_agent(self):
        agents = [
            {"pane_id": "p1", "agent_status": "idle", "focused": True, "title": "Hello There"},
            {"pane_id": "p2", "agent_status": "done", "focused": False, "title": "Slack Hits"},
            {"pane_id": "p3", "agent_status": "idle", "focused": False, "title": "Seen"},
        ]
        self.assertEqual(pick_next_waiter(agents)["pane_id"], "p2")

    def test_remote_with_no_sibling_uses_keystrokes(self):
        client = MagicMock()
        client.call_socket.return_value = {"result": {"snapshot": {"focused_pane_id": "p1"}}}
        with patch("agent_queue.native_jump.sys.platform", "darwin"), \
             patch("agent_queue.native_jump._wezterm", return_value="/wezterm"), \
             patch("agent_queue.native_jump.next_agent_sequence", return_value=[b"]"]), \
             patch("agent_queue.native_jump.find_wezterm_pane", return_value=7), \
             patch("agent_queue.native_jump._screen", return_value=" new · notebook   ● menu\n"), \
             patch("agent_queue.native_jump._jump_remote", return_value=False), \
             patch("agent_queue.native_jump.focus_sibling") as sibling, \
             patch("agent_queue.native_jump.keystroke_to_waiting", return_value=True) as keys, \
             patch("agent_queue.native_jump.time.sleep"):
            self.assertTrue(jump_to_waiting(client))
        sibling.assert_not_called()
        keys.assert_called_once_with(client, remember=False)

    def test_notebook_asks_mac_when_no_sibling_is_waiting(self):
        client = MagicMock()
        with patch("agent_queue.native_jump.sys.platform", "linux"), \
             patch("agent_queue.native_jump.focus_sibling", return_value=False), \
             patch("agent_queue.native_jump._focused_agent", return_value={"pane_id": "p-hello", "title": "Hello There"}), \
             patch("agent_queue.native_jump.push_back") as push, \
             patch("agent_queue.native_jump.set_waiting_view", return_value=True), \
             patch("agent_queue.native_jump.restore_this_space"), \
             patch("agent_queue.native_jump._ask_mac", return_value=True) as ask, \
             patch("agent_queue.native_jump.time.sleep"):
            self.assertTrue(jump_to_waiting(client))
        push.assert_called_once()
        self.assertEqual(ask.call_args.args[0]["op"], "next")
        self.assertEqual(ask.call_args.args[0]["left"]["pane_id"], "p-hello")

    def test_notebook_back_to_mac_uses_the_bridge(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        client = MagicMock()
        with patch("agent_queue.native_jump.BACK_FILE", path), \
             patch("agent_queue.native_jump.sys.platform", "linux"), \
             patch("agent_queue.native_jump._pane_on_server", return_value=False), \
             patch("agent_queue.native_jump._ask_mac", return_value=True) as ask:
            push_back({"pane_id": "wE:p45", "title": "Update Matt Pocock Skills"}, "Local")
            self.assertTrue(jump_back(client))
        self.assertEqual(ask.call_args.args[0]["op"], "focus")
        self.assertEqual(ask.call_args.args[0]["pane_id"], "wE:p45")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_undo_keeps_the_session_for_redo(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        client = MagicMock()
        client.call_socket.return_value = {"result": {"agents": [
            {"pane_id": "p-now", "tab_id": "t-now", "workspace_id": "wE", "focused": True, "title": "Now"},
            {"pane_id": "p-old", "tab_id": "t-old", "workspace_id": "wE", "focused": False, "title": "Old"},
        ]}}
        with patch("agent_queue.native_jump.BACK_FILE", path), \
             patch("agent_queue.native_jump.client_showing_local", return_value=True), \
             patch("agent_queue.native_jump.here_name", return_value="Local"):
            push_back({"pane_id": "p-old", "tab_id": "t-old", "workspace_id": "wE", "title": "Old"}, "Local")
            self.assertTrue(jump_back(client))
        with open(self.forward_path, encoding="utf-8") as f:
            forward = json.load(f)
        self.assertEqual(forward[0]["pane_id"], "p-now")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_next_redoes_when_nobody_is_waiting(self):
        client = MagicMock()
        client.call_socket.return_value = {"result": {"agents": [
            {"pane_id": "p-now", "tab_id": "t-now", "workspace_id": "wE", "focused": True, "title": "Now"},
            {"pane_id": "p-fwd", "tab_id": "t-fwd", "workspace_id": "wE", "focused": False, "title": "Forward"},
        ]}}
        fd, back = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, back)
        with patch("agent_queue.native_jump.BACK_FILE", back), \
             patch("agent_queue.native_jump._jump_waiting", return_value=False), \
             patch("agent_queue.native_jump.client_showing_local", return_value=True), \
             patch("agent_queue.native_jump.here_name", return_value="Local"):
            with open(self.forward_path, "w", encoding="utf-8") as f:
                json.dump([{
                    "machine": "Local", "pane_id": "p-fwd", "tab_id": "t-fwd",
                    "workspace_id": "wE", "title": "Forward",
                }], f)
            self.assertTrue(jump_to_waiting(client))
        client.call_socket.assert_any_call("agent.focus", {"target": "p-fwd"})
        with open(back, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["pane_id"], "p-now")
        with open(self.forward_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_back_stack_focuses_the_agent_you_left(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        client = MagicMock()
        client.call_socket.return_value = {"result": {"agents": [
            {"pane_id": "p-hello", "tab_id": "t-hello", "workspace_id": "wT"},
        ]}}
        with patch("agent_queue.native_jump.BACK_FILE", path), \
             patch("agent_queue.native_jump.client_showing_local", return_value=True):
            push_back({"pane_id": "p-hello", "tab_id": "t-hello", "workspace_id": "wT", "title": "Hello There"}, "Local")
            self.assertTrue(jump_back(client))
        client.call_socket.assert_any_call("tab.focus", {"tab_id": "t-hello"})
        screen = "  ▾ notebook         ●\n new · notebook   ● menu\n"
        self.assertEqual(header_machine(sidebar_text(screen)), "notebook")
        local = " new · Local   ● menu\n"
        self.assertEqual(header_machine(local), "Local")
        screen = "  Hello There       │ chatting about Hello There\n  Codebase Check    │\n"
        side = sidebar_text(screen)
        self.assertIn("Hello There", side)
        self.assertNotIn("chatting", side)

    def test_pick_when_the_open_session_was_closed(self):
        agents = [
            {"pane_id": "p2", "agent_status": "done", "focused": False, "title": "Next"},
            {"pane_id": "p3", "agent_status": "idle", "focused": False, "title": "Seen"},
        ]
        self.assertEqual(pick_next_waiter(agents)["pane_id"], "p2")

    def test_empty_next_returns_to_the_session_a_manual_switch_left(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        client = MagicMock()
        client.call_socket.return_value = {"result": {"agents": [
            {"pane_id": "p-now", "tab_id": "t-now", "workspace_id": "w", "title": "Now", "focused": True},
            {"pane_id": "p-left", "tab_id": "t-left", "workspace_id": "w", "title": "Left", "focused": False},
        ]}}
        with patch("agent_queue.native_jump.BACK_FILE", path), \
             patch("agent_queue.native_jump.sys.platform", "darwin"), \
             patch("agent_queue.native_jump.client_showing_local", return_value=True), \
             patch("agent_queue.native_jump._jump_waiting", return_value=False):
            push_back({"pane_id": "p-left", "tab_id": "t-left", "workspace_id": "w", "title": "Left"}, "Local")
            self.assertTrue(jump_to_waiting(client))
            client.call_socket.assert_any_call("agent.focus", {"target": "p-left"})
            self.assertTrue(jump_to_waiting(client))
        client.call_socket.assert_any_call("agent.focus", {"target": "p-now"})

    def test_single_wezterm_pane_does_not_scan_processes(self):
        def run(cmd, **_kwargs):
            if cmd and cmd[0] == "ps":
                raise AssertionError(cmd)
            result = MagicMock()
            result.stdout = json.dumps([{"pane_id": 4, "tty_name": "/dev/ttys000"}])
            result.returncode = 0
            return result

        with patch("agent_queue.native_jump.subprocess.run", side_effect=run):
            self.assertEqual(_lookup_wezterm_pane("wezterm"), 4)

    def test_fresh_bridge_skips_the_slow_ping(self):
        client = MagicMock()
        with patch("agent_queue.native_jump.sys.platform", "darwin"), \
             patch("agent_queue.native_jump._ping_local", return_value=True), \
             patch("agent_queue.native_jump._bridge_fresh", return_value=True), \
             patch("agent_queue.native_jump._remote_bridge_ok") as ping:
            ensure_back_bridge(client)
        ping.assert_not_called()

    def test_ssh_uses_the_unix_socket_when_it_is_up(self):
        fd, path = tempfile.mkstemp(suffix=".sock")
        os.close(fd)
        os.remove(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        ready = threading.Event()

        def serve() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            server.settimeout(2)
            ready.set()
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                server.close()
                return
            buf = b""
            while b"\n" not in buf:
                buf += conn.recv(1024)
            conn.sendall(b'{"ok": true, "out": "no"}\n')
            conn.close()
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))
        with patch("agent_queue.native_jump.REMOTE_RPC", path), \
             patch("agent_queue.native_jump._ssh_slow") as slow:
            self.assertEqual(_ssh("wxomi@notebook", ["jump"]), "no")
        slow.assert_not_called()
        self.assertEqual(dispatch_remote(["hi"]), "ok")

    def test_pick_empty_when_nothing_else_is_waiting(self):
        agents = [
            {"pane_id": "p1", "agent_status": "done", "focused": True, "title": "Only"},
            {"pane_id": "p2", "agent_status": "idle", "focused": False, "title": "Seen"},
        ]
        self.assertIsNone(pick_next_waiter(agents))


if __name__ == "__main__":
    unittest.main()
