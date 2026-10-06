"""Cross-machine jump through herdr's own client navigation.

Plugins and the socket API cannot switch which machine the herdr client displays;
only client-side navigation can. So: narrow the agent view to waiting agents, type the
user's `keys.next_agent` binding into the terminal hosting the herdr client (WezTerm),
then restore the "this space" view. The view is shared by the sidebar and next/previous
navigation, and on a multi-machine client it applies to the combined agent list.

Built-in next_agent walks the sidebar in panel order. Once you are already on an
agent, the next press can land on a seen neighbor (for example "Hello There")
instead of the other agent that is still waiting. When the open tab is on a
machine that has another done/blocked agent, focus that agent through that
machine's Herdr socket. The first hop, from another machine, still uses next_agent.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from typing import Any

from agent_queue.client import HerdrClient
from agent_queue.config import STATE_DIR

HERDR_CONFIG = os.path.expanduser("~/.config/herdr/config.toml")
WEZTERM_SOCKET = os.path.expanduser("~/.local/share/wezterm/default-org.wezfurlong.wezterm")
WEZTERM_APP_BIN = "/Applications/WezTerm.app/Contents/MacOS/wezterm"

# seen=false is the dot on the workspace. Status is often already "idle" by the time you press Option+n.
WAITING_FILTER = {
    "op": "any",
    "filters": [
        {"op": "eq", "field": "seen", "value": False},
        {"op": "in", "field": "status", "values": ["blocked", "done"]},
    ],
}
WAITING_SORT = [{"field": "attention", "order": "desc"}]

# Must match herdr-space-agents/apply_view.py, which owns the normal sidebar view.
THIS_SPACE_SOURCE = "wxomi.current-space"
THIS_SPACE_LABEL = "this space"
THIS_SPACE_FILTER = {"op": "eq", "field": "workspace_id", "value": {"context": "current_workspace_id"}}

DEFAULT_PREFIX = "ctrl+b"

# Let the attached client observe the narrowed view before the chord arrives.
VIEW_SETTLE_SECONDS = 0.02
CHORD_GAP_SECONDS = 0.04
# Stop as soon as focus moves. Cap so a no-op press does not sit on the waiting filter.
FOCUS_WAIT_SECONDS = 0.35
FOCUS_POLL_SECONDS = 0.03
CONTROL_PATH = os.path.join(STATE_DIR, "ssh-cm.sock")
BACK_FILE = os.path.join(STATE_DIR, "back.json")
FORWARD_FILE = os.path.join(STATE_DIR, "forward.json")
EXPECT_FILE = os.path.join(STATE_DIR, "nav-expect.json")
BACK_SOCK = os.path.join(STATE_DIR, "back.sock")
BRIDGE_OK = os.path.join(STATE_DIR, "bridge-ok")
RPC_SOCK = os.path.join(STATE_DIR, "rpc.sock")
REMOTE_RPC = os.path.join(STATE_DIR, "remote.sock")
BRIDGE_CONTROL = os.path.join(STATE_DIR, "bridge-cm.sock")
REMOTE_RPC_PATH = "/home/wxomi/.local/state/herdr/plugins/wxomi.agent-queue/rpc.sock"

# Runs on the remote machine. argv: list | focus <pane> <tab> <workspace>
_REMOTE_PY = r"""
import base64, json, os, socket, sys

def call(method, params):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(os.path.expanduser("~/.config/herdr/herdr.sock"))
    s.sendall((json.dumps({"id": "q", "method": method, "params": params}) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf or b"{}")

mode = sys.argv[1]
if mode == "jump":
    agents = (call("agent.list", {}).get("result") or {}).get("agents") or []
    waiting = [a for a in agents if a.get("agent_status") in ("blocked", "done") and not a.get("focused")]
    if not waiting:
        print("no")
    else:
        waiting.sort(key=lambda a: (a.get("agent_status") != "blocked", a.get("pane_id") or ""))
        agent = waiting[0]
        left = next((a for a in agents if a.get("focused")), {})
        ws, tab = agent.get("workspace_id") or "", agent.get("tab_id") or ""
        if ws:
            call("workspace.focus", {"workspace_id": ws})
        call("agent.focus", {"target": agent.get("pane_id") or ""})
        if tab:
            call("tab.focus", {"tab_id": tab})
        print(json.dumps({
            "ok": True,
            "left": {
                "pane_id": left.get("pane_id") or "",
                "tab_id": left.get("tab_id") or "",
                "workspace_id": left.get("workspace_id") or "",
                "title": left.get("title") or "",
            },
            "title": agent.get("title") or "",
        }))
elif mode == "push":
    item = json.loads(base64.b64decode(sys.argv[2]))
    path = os.path.expanduser("~/.local/state/herdr/plugins/wxomi.agent-queue/back.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        items = json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        items = []
    if not isinstance(items, list):
        items = []
    items.append(item)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items[-30:], f)
    print("ok")
elif mode == "list":
    sys.stdout.write(json.dumps({
        "agents": call("agent.list", {}),
        "workspaces": call("workspace.list", {}),
    }))
elif mode == "view":
    seq = int(sys.argv[2])
    call("agent.view.set", {
        "source": "wxomi.agent-queue",
        "label": "back",
        "filter": {"op": "eq", "field": "state_change_seq", "value": seq},
    })
    print("ok")
elif mode == "restore":
    call("agent.view.set", {
        "source": "wxomi.current-space",
        "label": "this space",
        "filter": {"op": "eq", "field": "workspace_id", "value": {"context": "current_workspace_id"}},
    })
    print("ok")
elif mode == "ping":
    path = os.path.expanduser("~/.local/state/herdr/plugins/wxomi.agent-queue/back.sock")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(path)
        s.sendall(b'{"op":"ping"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(32)
            if not chunk:
                break
            buf += chunk
        s.close()
        print("ok" if buf.startswith(b"ok") else "no")
    except OSError:
        print("no")
else:
    pane, tab, ws = sys.argv[2], sys.argv[3], sys.argv[4]
    if ws:
        call("workspace.focus", {"workspace_id": ws})
    call("agent.focus", {"target": pane})
    if tab:
        call("tab.focus", {"tab_id": tab})
    print("ok")
"""


def _key_bytes(key: str) -> bytes | None:
    """Encode a single herdr key like `ctrl+b`, `]`, or `alt+n` as terminal input bytes."""
    parts = key.lower().split("+")
    base = parts[-1]
    mods = set(parts[:-1])
    if len(base) != 1 or mods - {"ctrl", "alt"}:
        return None
    out = base.encode()
    if "ctrl" in mods:
        if not base.isalpha():
            return None
        out = bytes([ord(base) & 0x1F])
    if "alt" in mods:
        out = b"\x1b" + out
    return out


def next_agent_sequence(config_path: str = HERDR_CONFIG) -> list[bytes] | None:
    """Keystrokes for the configured native next_agent binding, one chunk per chord."""
    try:
        with open(config_path, "rb") as f:
            keys = tomllib.load(f).get("keys", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    binding = keys.get("next_agent")
    if not isinstance(binding, str):
        return None
    chords: list[bytes] = []
    if binding.startswith("prefix+"):
        prefix = _key_bytes(str(keys.get("prefix") or DEFAULT_PREFIX))
        if prefix is None:
            return None
        chords.append(prefix)
        binding = binding[len("prefix+"):]
    key = _key_bytes(binding)
    if key is None:
        return None
    chords.append(key)
    return chords


def _wezterm() -> str | None:
    return shutil.which("wezterm") or (WEZTERM_APP_BIN if os.path.exists(WEZTERM_APP_BIN) else None)


def _wezterm_env() -> dict[str, str]:
    env = dict(os.environ)
    # Panes inherit a WEZTERM_UNIX_SOCKET from whichever GUI launched herdr, which goes
    # stale after WezTerm restarts; the default symlink tracks the live GUI.
    if os.path.exists(WEZTERM_SOCKET):
        env["WEZTERM_UNIX_SOCKET"] = WEZTERM_SOCKET
    return env


def herdr_client_ttys() -> set[str]:
    """TTYs of running herdr TUI clients (not the server or CLI helpers)."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "tty=,command="], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    ttys: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] in ("??", "-"):
            continue
        argv = parts[1:]
        if os.path.basename(argv[0]) != "herdr":
            continue
        if len(argv) == 1 or argv[1] in ("--session", "--remote") or argv[1:3] == ["session", "attach"]:
            ttys.add("/dev/" + parts[0])
    return ttys


_PANE_CACHE: tuple[float, int | None] = (0.0, None)


def find_wezterm_pane(wezterm: str) -> int | None:
    """Pane that owns the herdr window. Reused for 1s so one jump does not scan processes twice."""
    global _PANE_CACHE
    now = time.monotonic()
    if _PANE_CACHE[1] is not None and now - _PANE_CACHE[0] < 1.0:
        return _PANE_CACHE[1]
    pane = _lookup_wezterm_pane(wezterm)
    if pane is not None:
        _PANE_CACHE = (now, pane)
    return pane


def _wezterm_panes(wezterm: str) -> list[dict[str, Any]]:
    try:
        out = subprocess.run(
            [wezterm, "cli", "list", "--format", "json"],
            capture_output=True, text=True, timeout=3.0, check=False, env=_wezterm_env(),
        ).stdout
        panes = json.loads(out or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    return panes if isinstance(panes, list) else []


def _lookup_wezterm_pane(wezterm: str) -> int | None:
    panes = _wezterm_panes(wezterm)
    # One window: that pane is herdr. Skip the full process scan (about 35ms).
    if len(panes) == 1 and panes[0].get("pane_id") is not None:
        return int(panes[0]["pane_id"])
    ttys = herdr_client_ttys()
    if not ttys:
        return None
    matches = [p for p in panes if p.get("tty_name") in ttys]
    if not matches:
        return None
    matches.sort(key=lambda p: not p.get("is_active"))
    return int(matches[0]["pane_id"])


def _send(wezterm: str, pane_id: int, data: bytes) -> bool:
    try:
        res = subprocess.run(
            [wezterm, "cli", "send-text", "--pane-id", str(pane_id), "--no-paste"],
            input=data, capture_output=True, timeout=3.0, check=False, env=_wezterm_env(),
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def restore_this_space(client: HerdrClient) -> bool:
    res = client.call_socket(
        "agent.view.set",
        {"source": THIS_SPACE_SOURCE, "label": THIS_SPACE_LABEL, "filter": THIS_SPACE_FILTER},
    )
    return "result" in res


def set_waiting_view(client: HerdrClient) -> bool:
    res = client.call_socket(
        "agent.view.set",
        {"source": "wxomi.agent-queue", "label": "waiting", "filter": WAITING_FILTER, "sort": WAITING_SORT},
    )
    return "result" in res


def pick_next_waiter(agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Next done/blocked agent on this machine, skipping the one already focused.

    A killed session leaves nothing focused. Still take the first waiter.
    """
    waiting = [a for a in agents if a.get("agent_status") in ("blocked", "done") and not a.get("focused")]
    if not waiting:
        return None
    waiting.sort(key=lambda a: (a.get("agent_status") != "blocked", a.get("pane_id") or ""))
    return waiting[0]


def sidebar_text(screen: str) -> str:
    """Herdr's left column only. The pane on the right can mention an agent name without it being open."""
    rows = []
    for line in screen.splitlines():
        rows.append(line.split("│", 1)[0])
    return "\n".join(rows)


def _screen(wezterm: str, pane_id: int) -> str:
    try:
        res = subprocess.run(
            [wezterm, "cli", "get-text", "--pane-id", str(pane_id), "--start-line", "0", "--end-line", "40"],
            capture_output=True, text=True, timeout=2.0, check=False, env=_wezterm_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = res.stdout or ""
    return out if isinstance(out, str) else ""


def _focus_local(client: HerdrClient, agent: dict[str, Any]) -> bool:
    if agent.get("workspace_id"):
        client.call_socket("workspace.focus", {"workspace_id": agent["workspace_id"]})
    res = client.call_socket("agent.focus", {"target": agent["pane_id"]})
    if agent.get("tab_id"):
        client.call_socket("tab.focus", {"tab_id": agent["tab_id"]})
    return "result" in res


def _herdr_call(method: str, params: dict | None = None) -> dict:
    """One Herdr socket call. Used by the notebook RPC server."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(os.path.expanduser("~/.config/herdr/herdr.sock"))
        sock.sendall((json.dumps({"id": "q", "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    return json.loads(buf or b"{}")


def dispatch_remote(argv: list[str]) -> str:
    """Notebook-side command. Same results as the one-shot SSH script."""
    if not argv:
        return ""
    mode = argv[0]
    if mode == "hi":
        return "ok"
    if mode == "jump":
        agents = (_herdr_call("agent.list").get("result") or {}).get("agents") or []
        waiting = [a for a in agents if a.get("agent_status") in ("blocked", "done") and not a.get("focused")]
        if not waiting:
            return "no"
        waiting.sort(key=lambda a: (a.get("agent_status") != "blocked", a.get("pane_id") or ""))
        agent = waiting[0]
        left = next((a for a in agents if a.get("focused")), {})
        ws, tab = agent.get("workspace_id") or "", agent.get("tab_id") or ""
        if ws:
            _herdr_call("workspace.focus", {"workspace_id": ws})
        _herdr_call("agent.focus", {"target": agent.get("pane_id") or ""})
        if tab:
            _herdr_call("tab.focus", {"tab_id": tab})
        return json.dumps({
            "ok": True,
            "left": {
                "pane_id": left.get("pane_id") or "",
                "tab_id": left.get("tab_id") or "",
                "workspace_id": left.get("workspace_id") or "",
                "title": left.get("title") or "",
            },
            "title": agent.get("title") or "",
        })
    if mode == "agents":
        return json.dumps(_herdr_call("agent.list"))
    if mode == "push":
        item = json.loads(base64.b64decode(argv[1]))
        path = os.path.expanduser("~/.local/state/herdr/plugins/wxomi.agent-queue/back.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            items = json.loads(open(path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError):
            items = []
        if not isinstance(items, list):
            items = []
        items.append(item)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items[-30:], f)
        return "ok"
    if mode == "list":
        return json.dumps({
            "agents": _herdr_call("agent.list"),
            "workspaces": _herdr_call("workspace.list"),
        })
    if mode == "seq":
        pane = argv[1] if len(argv) > 1 else ""
        agents = (_herdr_call("agent.list").get("result") or {}).get("agents") or []
        for agent in agents:
            if agent.get("pane_id") == pane and agent.get("state_change_seq"):
                seq = int(agent["state_change_seq"])
                _herdr_call("agent.view.set", {
                    "source": "wxomi.agent-queue",
                    "label": "back",
                    "filter": {"op": "eq", "field": "state_change_seq", "value": seq},
                })
                return str(seq)
        return ""
    if mode == "view":
        _herdr_call("agent.view.set", {
            "source": "wxomi.agent-queue",
            "label": "back",
            "filter": {"op": "eq", "field": "state_change_seq", "value": int(argv[1])},
        })
        return "ok"
    if mode == "restore":
        _herdr_call("agent.view.set", {
            "source": "wxomi.current-space",
            "label": "this space",
            "filter": {"op": "eq", "field": "workspace_id", "value": {"context": "current_workspace_id"}},
        })
        return "ok"
    if mode == "ping":
        path = os.path.expanduser("~/.local/state/herdr/plugins/wxomi.agent-queue/back.sock")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ping:
                ping.settimeout(2)
                ping.connect(path)
                ping.sendall(b'{"op":"ping"}\n')
                buf = b""
                while b"\n" not in buf:
                    chunk = ping.recv(32)
                    if not chunk:
                        break
                    buf += chunk
            return "ok" if buf.startswith(b"ok") else "no"
        except OSError:
            return "no"
    if len(argv) < 4:
        return ""
    pane, tab, ws = argv[1], argv[2], argv[3]
    if ws:
        _herdr_call("workspace.focus", {"workspace_id": ws})
    _herdr_call("agent.focus", {"target": pane})
    if tab:
        _herdr_call("tab.focus", {"tab_id": tab})
    return "ok"


def rpc_up(path: str | None = None) -> bool:
    """True when a command socket is accepting `hi`."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            s.connect(path or RPC_SOCK)
            s.sendall(b'{"args":["hi"]}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(64)
                if not chunk:
                    break
                buf += chunk
        return b"ok" in buf
    except OSError:
        return False


def serve_rpc() -> None:
    """Stay up and answer jump/list/view calls. One process, no Python startup per hop."""
    if rpc_up():
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(RPC_SOCK):
        try:
            os.remove(RPC_SOCK)
        except OSError:
            return
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(RPC_SOCK)
    except OSError:
        server.close()
        return
    server.listen(16)
    server.settimeout(1.0)
    while True:
        try:
            conn, _addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        # One held-open client must not block the next Option+n process.
        threading.Thread(target=_serve_rpc_conn, args=(conn,), daemon=True).start()


def _serve_rpc_conn(conn: socket.socket) -> None:
    with conn:
        buf = b""
        while len(buf) < 200_000:
            while b"\n" not in buf:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
            line, _, buf = buf.partition(b"\n")
            try:
                item = json.loads(line.decode() or "{}")
                args = item.get("args") if isinstance(item, dict) else None
                if not isinstance(args, list):
                    conn.sendall(b'{"ok":false}\n')
                    continue
                out = dispatch_remote([str(a) for a in args])
                conn.sendall((json.dumps({"ok": True, "out": out}) + "\n").encode())
            except (OSError, json.JSONDecodeError, ValueError, KeyError):
                try:
                    conn.sendall(b'{"ok":false}\n')
                except OSError:
                    return
                return


_rpc_sock: socket.socket | None = None


def _rpc_call(args: list[str]) -> str | None:
    """Talk to notebook through one kept-open Unix socket. None if it is down."""
    global _rpc_sock
    if not os.path.exists(REMOTE_RPC):
        return None
    payload = (json.dumps({"args": args}) + "\n").encode()
    for _attempt in range(2):
        try:
            if _rpc_sock is None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(0.4)
                sock.connect(REMOTE_RPC)
                _rpc_sock = sock
            _rpc_sock.sendall(payload)
            buf = b""
            while b"\n" not in buf:
                chunk = _rpc_sock.recv(65536)
                if not chunk:
                    raise OSError("closed")
                buf += chunk
            msg = json.loads(buf.split(b"\n", 1)[0].decode() or "{}")
        except TimeoutError:
            if _rpc_sock is not None:
                try:
                    _rpc_sock.close()
                except OSError:
                    pass
            _rpc_sock = None
            return None
        except (OSError, json.JSONDecodeError):
            if _rpc_sock is not None:
                try:
                    _rpc_sock.close()
                except OSError:
                    pass
            _rpc_sock = None
            continue
        if not isinstance(msg, dict) or not msg.get("ok"):
            return None
        return msg.get("out") or ""
    return None


def _ssh_slow(target: str, args: list[str]) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        res = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                "-o", "ControlMaster=auto", "-o", "ControlPersist=300",
                "-o", f"ControlPath={CONTROL_PATH}",
                target, "python3", "-", *args,
            ],
            input=_REMOTE_PY, capture_output=True, text=True, timeout=4.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if res.returncode != 0:
        return ""
    return res.stdout or ""


def _ssh(target: str, args: list[str]) -> str:
    fast = _rpc_call(args)
    if fast is not None:
        return fast
    return _ssh_slow(target, args)


def header_machine(sidebar: str) -> str:
    """Machine name from Herdr's `new · notebook · menu` header. Empty if it is not on screen."""
    for line in sidebar.splitlines():
        if "·" not in line or "menu" not in line:
            continue
        name = line.split("·", 1)[1]
        for junk in ("●", "○", "menu"):
            name = name.replace(junk, "")
        return name.strip()
    return ""


def _read_back() -> list[dict[str, str]]:
    try:
        with open(BACK_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_back(items: list[dict[str, str]]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(BACK_FILE, "w", encoding="utf-8") as f:
        json.dump(items[-30:], f)


def _read_stack(path: str) -> list[dict[str, str]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_stack(path: str, items: list[dict[str, str]]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items[-30:], f)


def clear_forward() -> None:
    """A new path drops the sessions Option+p had undone."""
    _write_stack(FORWARD_FILE, [])


def push_forward(agent: dict[str, Any], machine: str) -> None:
    pane = agent.get("pane_id") or ""
    if not pane:
        return
    items = _read_stack(FORWARD_FILE)
    items.append({
        "machine": machine,
        "pane_id": pane,
        "tab_id": agent.get("tab_id") or "",
        "workspace_id": agent.get("workspace_id") or "",
        "title": agent.get("title") or "",
    })
    _write_stack(FORWARD_FILE, items)


def expect_navigation(pane_id: str) -> None:
    """Tell the daemon this focus change is ours, so it is not a manual switch."""
    if not pane_id:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(EXPECT_FILE, "w", encoding="utf-8") as f:
        json.dump({"pane_id": pane_id, "until": time.time() + 5}, f)


def cancel_expect() -> None:
    try:
        os.remove(EXPECT_FILE)
    except OSError:
        pass


def take_expected(pane_id: str) -> bool:
    try:
        with open(EXPECT_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("until", 0) < time.time():
        cancel_expect()
        return False
    if data.get("pane_id") != pane_id:
        return False
    cancel_expect()
    return True


def remember_back(agent: dict[str, Any], machine: str) -> None:
    """Save the session the user just left. Skip if it is already the top of the stack."""
    pane = agent.get("pane_id") or ""
    if not pane:
        return
    items = _read_back()
    if items and items[-1].get("pane_id") == pane and (items[-1].get("machine") or "") == machine:
        return
    push_back(agent, machine)


def push_back(agent: dict[str, Any], machine: str) -> None:
    pane = agent.get("pane_id") or ""
    if not pane:
        return
    items = _read_back()
    items.append({
        "machine": machine,
        "pane_id": pane,
        "tab_id": agent.get("tab_id") or "",
        "workspace_id": agent.get("workspace_id") or "",
        "title": agent.get("title") or "",
    })
    _write_back(items)


def publish_back(client: HerdrClient, agent: dict[str, Any], machine: str) -> None:
    """Remember this agent here and on each saved machine, so Option+p can run there."""
    push_back(agent, machine)
    import base64
    item = base64.b64encode(json.dumps(items_last(agent, machine)).encode()).decode()
    for _label, target in machine_targets(client.bin):
        _ssh(target, ["push", item])


def items_last(agent: dict[str, Any], machine: str) -> dict[str, str]:
    return {
        "machine": machine,
        "pane_id": agent.get("pane_id") or "",
        "tab_id": agent.get("tab_id") or "",
        "workspace_id": agent.get("workspace_id") or "",
        "title": agent.get("title") or "",
    }


def _focused_agent(client: HerdrClient) -> dict[str, Any] | None:
    res = client.call_socket("agent.list")
    if not isinstance(res, dict):
        return None
    agents = (res.get("result") or {}).get("agents") or []
    if not isinstance(agents, list):
        return None
    return next((a for a in agents if a.get("focused") and a.get("pane_id")), None)


def focus_by_view(client: HerdrClient, pane_id: str, title: str) -> bool:
    """Point the client at one pane, including when that pane is on the other machine."""
    wezterm = _wezterm()
    term_pane = find_wezterm_pane(wezterm) if wezterm else None
    chords = next_agent_sequence()
    if not wezterm or term_pane is None or not chords or not pane_id:
        return False
    res = client.call_socket(
        "agent.view.set",
        {
            "source": "wxomi.agent-queue",
            "label": "back",
            "filter": {"op": "eq", "field": "pane_id", "value": pane_id},
        },
    )
    if "result" not in res:
        return False
    try:
        time.sleep(VIEW_SETTLE_SECONDS)
        for i, chord in enumerate(chords):
            if i:
                time.sleep(CHORD_GAP_SECONDS)
            if not _send(wezterm, term_pane, chord):
                return False
        time.sleep(0.25)
        return True
    finally:
        restore_this_space(client)


def _ask_mac(target: dict[str, Any]) -> bool:
    """Ask the Mac client to move. Notebook reaches it through the reverse SSH socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(4)
            s.connect(BACK_SOCK)
            s.sendall((json.dumps(target) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(64)
                if not chunk:
                    break
                buf += chunk
            return buf.startswith(b"ok")
    except OSError:
        return False


def _serve_back() -> None:
    try:
        if os.path.exists(BACK_SOCK):
            os.remove(BACK_SOCK)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(BACK_SOCK)
        server.listen(2)
        server.settimeout(1.0)
    except OSError:
        return
    while True:
        try:
            conn, _addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        with conn:
            buf = b""
            while b"\n" not in buf and len(buf) < 8000:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                buf += chunk
            try:
                item = json.loads(buf.decode() or "{}")
            except json.JSONDecodeError:
                conn.sendall(b"no\n")
                continue
            if not isinstance(item, dict):
                conn.sendall(b"no\n")
                continue
            op = item.get("op") or ("focus" if item.get("pane_id") else "ping")
            if op == "ping":
                conn.sendall(b"ok\n")
                continue
            client = HerdrClient()
            if op == "keys":
                ok = send_next_keys()
            elif op == "next":
                left = item.get("left") or {}
                if isinstance(left, dict) and left.get("pane_id"):
                    push_back(left, left.get("machine") or "notebook")
                    _log(f"bridge next saved {left.get('title')} stack={len(_read_back())}")
                else:
                    _log("bridge next saved nothing")
                ok = keystroke_to_waiting(client, remember=False)
            elif op == "prev":
                ok = jump_back(client)
            else:
                _log(f"bridge {op} pane={item.get('pane_id')} title={item.get('title')}")
                ok = focus_by_seq(client, item.get("pane_id") or "", item.get("title") or "", item.get("machine") or "")
            conn.sendall(b"ok\n" if ok else b"no\n")


_listener_thread: threading.Thread | None = None


def _log(msg: str) -> None:
    if os.environ.get("HERDR_QUEUE_NO_BRIDGE"):
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "jump.log")
        if os.path.exists(path) and os.path.getsize(path) > 100_000:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _ping_local() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            s.connect(BACK_SOCK)
            s.sendall(b'{"op":"ping"}\n')
            return s.recv(16).startswith(b"ok")
    except OSError:
        return False


def remote_agents(machine: str, herdr: str) -> list[dict[str, Any]] | None:
    """Agent list through the Unix socket. None when the socket is down."""
    labels = [label.lower() for label, _target in machine_targets(herdr)]
    if labels and machine.lower() not in labels:
        return None
    raw = _rpc_call(["agents"])
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    agents = (data.get("result") or {}).get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, list):
        return None
    return agents


def _remote_bridge_ok(client: HerdrClient) -> bool:
    for _label, target in machine_targets(client.bin):
        if _ssh_slow(target, ["ping"]).strip() == "ok":
            return True
    return False


def _restart_reverse_tunnel(client: HerdrClient) -> None:
    remote = "/home/wxomi/.local/state/herdr/plugins/wxomi.agent-queue/back.sock"
    for _label, target in machine_targets(client.bin):
        if os.path.exists(BRIDGE_CONTROL):
            try:
                subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", f"ControlPath={BRIDGE_CONTROL}", "-O", "exit", target],
                    capture_output=True, timeout=3.0, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                os.remove(BRIDGE_CONTROL)
            except OSError:
                pass
        # A leftover socket with nobody listening makes ssh -R fail until it is removed.
        try:
            subprocess.run(
                [
                    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                    "-o", "ControlMaster=auto", "-o", "ControlPersist=300",
                    "-o", f"ControlPath={CONTROL_PATH}",
                    target, "rm", "-f", remote,
                ],
                capture_output=True, timeout=4.0, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            res = subprocess.run(
                [
                    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                    "-o", "ControlMaster=yes", "-o", "ControlPersist=300",
                    "-o", f"ControlPath={BRIDGE_CONTROL}",
                    "-o", "StreamLocalBindUnlink=yes", "-o", "ExitOnForwardFailure=yes",
                    "-N", "-f",
                    "-R", f"{remote}:{BACK_SOCK}",
                    "-L", f"{REMOTE_RPC}:{REMOTE_RPC_PATH}",
                    target,
                ],
                capture_output=True, text=True, timeout=5.0, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"bridge ssh failed: {exc}")
            continue
        if res.returncode != 0:
            _log(f"bridge ssh {res.returncode}: {(res.stderr or '').strip()[:200]}")


def _start_remote_rpc(client: HerdrClient) -> bool:
    """Start notebook's command socket if the synced code can serve it."""
    script = (
        "cd ~/.local/share/herdr-agent-queue || exit 2; "
        "export PYTHONPATH=.; "
        "python3 -c 'from agent_queue.native_jump import rpc_up; raise SystemExit(0 if rpc_up() else 1)' && exit 0; "
        "nohup python3 -m agent_queue --rpc >/dev/null 2>&1 & exit 0"
    )
    ok = False
    for _label, target in machine_targets(client.bin):
        try:
            res = subprocess.run(
                [
                    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                    "-o", "ControlMaster=auto", "-o", "ControlPersist=300",
                    "-o", f"ControlPath={CONTROL_PATH}",
                    target, script,
                ],
                capture_output=True, timeout=4.0, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if res.returncode == 0:
            ok = True
    return ok


def _bridge_fresh() -> bool:
    """True for 20s after a healthy check, so a keypress does not SSH just to look."""
    try:
        return time.time() - os.path.getmtime(BRIDGE_OK) < 20
    except OSError:
        return False


def _mark_bridge() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(BRIDGE_OK, "a", encoding="utf-8"):
            pass
        os.utime(BRIDGE_OK, None)
    except OSError:
        pass


def ensure_back_bridge(client: HerdrClient) -> None:
    """Mac listens. Notebook reaches that listener, and the Mac reaches notebook's command socket."""
    global _listener_thread
    if sys.platform != "darwin" or os.environ.get("HERDR_QUEUE_NO_BRIDGE"):
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    if _ping_local() and _bridge_fresh():
        return
    if not _ping_local():
        if _listener_thread is None or not _listener_thread.is_alive():
            _listener_thread = threading.Thread(target=_serve_back, daemon=True)
            _listener_thread.start()
            for _ in range(20):
                if _ping_local():
                    break
                time.sleep(0.02)
    back_ok = _remote_bridge_ok(client)
    fast_ok = _rpc_call(["hi"]) is not None
    if back_ok and fast_ok:
        _mark_bridge()
        return
    if not fast_ok and not _start_remote_rpc(client) and back_ok:
        return
    _restart_reverse_tunnel(client)


def _toast(client: HerdrClient, title: str, body: str) -> None:
    client.show_toast(title or body, body=body, sound="none", position="top-right")


def here_name() -> str:
    """Machine label stored in the back stack for this server."""
    if sys.platform == "darwin":
        return "Local"
    return os.environ.get("HERDR_QUEUE_MACHINE") or "notebook"


def client_showing_local() -> bool:
    """True when the herdr window is on Local, so a socket focus can move it."""
    if os.environ.get("HERDR_QUEUE_NO_BRIDGE") or sys.platform != "darwin":
        return sys.platform == "darwin"
    wezterm = _wezterm()
    pane_id = find_wezterm_pane(wezterm) if wezterm else None
    if not wezterm or pane_id is None:
        return True
    name = header_machine(sidebar_text(_screen(wezterm, pane_id))).lower()
    return name in ("", "local")


def _pane_on_server(client: HerdrClient, pane_id: str) -> bool:
    res = client.call_socket("agent.list")
    agents = ((res.get("result") or {}).get("agents") or []) if isinstance(res, dict) else []
    return any(a.get("pane_id") == pane_id for a in agents)


def send_next_keys() -> bool:
    """Type herdr's next_agent binding into the WezTerm pane that owns the client."""
    wezterm = _wezterm()
    pane_id = find_wezterm_pane(wezterm) if wezterm else None
    chords = next_agent_sequence()
    if not wezterm or pane_id is None or not chords:
        return False
    for i, chord in enumerate(chords):
        if i:
            time.sleep(CHORD_GAP_SECONDS)
        if not _send(wezterm, pane_id, chord):
            return False
    return True


def keystroke_to_waiting(client: HerdrClient, remember: bool = True) -> bool:
    """Narrow the list to waiting agents and press next_agent. This is what switches machines."""
    wezterm = _wezterm()
    pane_id = find_wezterm_pane(wezterm) if wezterm else None
    if not wezterm or pane_id is None or not next_agent_sequence():
        return False
    current = _focused_agent(client) if remember else None
    before = focused_place(client)
    if not set_waiting_view(client):
        _log("next keystroke view failed")
        return False
    _log("next keystroke")
    try:
        time.sleep(VIEW_SETTLE_SECONDS)
        if "no matching agents" in _screen(wezterm, pane_id).lower():
            _log("next: nobody waiting")
            return False
        if not send_next_keys():
            return False
        deadline = time.monotonic() + FOCUS_WAIT_SECONDS
        while time.monotonic() < deadline:
            if focused_place(client) != before:
                # Restoring the sidebar in the same instant snaps the client back.
                time.sleep(0.25)
                if current:
                    publish_back(client, current, "Local")
                    ensure_back_bridge(client)
                return True
            time.sleep(FOCUS_POLL_SECONDS)
        return False
    finally:
        restore_this_space(client)


def focus_sibling(client: HerdrClient) -> bool:
    """Focus the other done/blocked agent on this server. No keystrokes."""
    res = client.call_socket("agent.list")
    if not isinstance(res, dict):
        return False
    agents = (res.get("result") or {}).get("agents") or []
    if not isinstance(agents, list):
        return False
    nxt = pick_next_waiter(agents)
    if not nxt:
        return False
    focused = next((a for a in agents if a.get("focused")), None)
    if focused:
        push_back(focused, here_name())
    if not _focus_local(client, nxt):
        return False
    _toast(client, nxt.get("title") or "", "Next waiting agent")
    return True


def machine_targets(herdr: str) -> list[tuple[str, str]]:
    """(label, ssh target) for enabled saved machines. Cached for 30s."""
    now = time.monotonic()
    cached = getattr(machine_targets, "_cache", None)
    if cached and cached[1] == herdr and now - cached[0] < 30:
        return cached[2]
    try:
        out = subprocess.run([herdr, "machine", "list"], capture_output=True, text=True, timeout=3.0, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return cached[2] if cached else []
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5 and parts[4].strip() == "enabled" and parts[1].strip() and parts[2].strip():
            found.append((parts[1].strip(), parts[2].strip()))
    machine_targets._cache = (now, herdr, found)  # type: ignore[attr-defined]
    return found


def focused_place(client: HerdrClient) -> tuple[str | None, str | None]:
    """(focused_pane_id, focused_workspace_id) from the local server snapshot."""
    res = client.call_socket("session.snapshot")
    snap = (res.get("result") or {}).get("snapshot") or {}
    return snap.get("focused_pane_id"), snap.get("focused_workspace_id")


def _jump_remote(client: HerdrClient, machine: str) -> bool:
    """One SSH: pick and focus the other waiter on that machine."""
    for label, target in machine_targets(client.bin):
        if label.lower() != machine.lower():
            continue
        raw = _ssh(target, ["jump"])
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not data.get("ok"):
            return False
        left = data.get("left") or {}
        if left.get("pane_id"):
            publish_back(client, left, machine)
        _toast(client, data.get("title") or "", "Next waiting agent")
        return True
    return False


def client_typing_fingerprint() -> str:
    """Right side of the herdr window, where the user types. Sidebar dots are left out."""
    wezterm = _wezterm()
    pane_id = find_wezterm_pane(wezterm) if wezterm else None
    if not wezterm or pane_id is None:
        return ""
    try:
        res = subprocess.run(
            [wezterm, "cli", "get-text", "--pane-id", str(pane_id), "--start-line", "-25", "--end-line", "-1"],
            capture_output=True, text=True, timeout=2.0, check=False, env=_wezterm_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    parts = []
    for line in (res.stdout or "").splitlines():
        parts.append(line.split("│", 1)[1] if "│" in line else line)
    return "\n".join(parts).rstrip()


def screen_machine() -> str:
    wezterm = _wezterm()
    pane_id = find_wezterm_pane(wezterm) if wezterm else None
    if not wezterm or pane_id is None:
        return ""
    return header_machine(sidebar_text(_screen(wezterm, pane_id))).lower()


def find_seq(client: HerdrClient, pane_id: str) -> int | None:
    """state_change_seq is the only view field that matches an agent on the other machine."""
    find_seq.remote_armed = False  # type: ignore[attr-defined]
    res = client.call_socket("agent.list")
    agents = ((res.get("result") or {}).get("agents") or []) if isinstance(res, dict) else []
    for agent in agents:
        if agent.get("pane_id") == pane_id and agent.get("state_change_seq"):
            return int(agent["state_change_seq"])
    for _label, target in machine_targets(client.bin):
        raw = (_ssh(target, ["seq", pane_id]) or "").strip()
        if raw.isdigit():
            find_seq.remote_armed = True  # type: ignore[attr-defined]
            return int(raw)
        try:
            data = json.loads(_ssh(target, ["list"]) or "{}")
        except json.JSONDecodeError:
            continue
        remote = ((data.get("agents") or {}).get("result") or {}).get("agents") or []
        for agent in remote:
            if agent.get("pane_id") == pane_id and agent.get("state_change_seq"):
                return int(agent["state_change_seq"])
    return None


def _set_seq_view(client: HerdrClient, seq: int) -> bool:
    res = client.call_socket(
        "agent.view.set",
        {
            "source": "wxomi.agent-queue",
            "label": "back",
            "filter": {"op": "eq", "field": "state_change_seq", "value": seq},
        },
    )
    return "result" in res


def open_finished(client: HerdrClient, machine: str, agent: dict[str, Any], left: dict[str, Any] | None) -> bool:
    """Autopilot: you are idle, and this agent just finished. Open it."""
    pane = agent.get("pane_id") or ""
    expect_navigation(pane)
    if left and left.get("pane_id") and left.get("pane_id") != pane:
        push_back(left, left.get("machine") or "Local")
    ok = focus_by_seq(client, pane, agent.get("title") or "", machine)
    if not ok:
        cancel_expect()
        return False
    clear_forward()
    _toast(client, agent.get("title") or "Agent finished", "Autopilot")
    return True


def _move_goal(before: str, machine: str) -> str:
    """Machine the header must show. Empty when the jump stays on this machine."""
    want = (machine or "").lower()
    if want and want not in ("local", before):
        return want
    if want in ("", "local") and before not in ("", "local"):
        return "local"
    return ""


def _wait_for_jump(client: HerdrClient, before: str, machine: str) -> bool:
    """Return as soon as the window moves. Cap at 0.25s, same as the old fixed wait."""
    goal = _move_goal(before, machine)
    before_place = focused_place(client)
    wezterm = _wezterm() if goal else None
    pane = find_wezterm_pane(wezterm) if wezterm else None
    deadline = time.monotonic() + 0.25
    while True:
        if goal and wezterm and pane is not None:
            after = header_machine(sidebar_text(_screen(wezterm, pane))).lower()
            if goal == "local":
                if after in ("", "local"):
                    return True
            elif after == goal:
                return True
        elif not goal and focused_place(client) != before_place:
            return True
        if time.monotonic() >= deadline:
            return goal == ""
        time.sleep(0.03)


def focus_by_seq(client: HerdrClient, pane_id: str, title: str, machine: str = "") -> bool:
    """Show one agent, on either machine, then press next_agent."""
    seq = find_seq(client, pane_id)
    armed = bool(getattr(find_seq, "remote_armed", False))
    _log(f"prev focus pane={pane_id} seq={seq} machine={machine} title={title}")
    if not seq or not _set_seq_view(client, seq):
        return False
    targets = machine_targets(client.bin)
    if not armed:
        for _label, target in targets:
            _ssh(target, ["view", str(seq)])
    before = screen_machine()
    try:
        time.sleep(VIEW_SETTLE_SECONDS)
        if not send_next_keys():
            _log("prev keys failed")
            return False
        ok = _wait_for_jump(client, before, machine)
        if ok:
            # Header flips before the workspace context does. Restoring earlier snaps back.
            time.sleep(0.25)
        _log(f"prev header {before or 'local'} ok={ok}")
        return ok
    finally:
        restore_this_space(client)
        for _label, target in targets:
            _ssh(target, ["restore"])


def _handoff_next(client: HerdrClient) -> bool:
    """Notebook has no other waiter. Ask the Mac to move the window to the next one."""
    focused = _focused_agent(client)
    if focused:
        push_back(focused, here_name())
    set_waiting_view(client)
    try:
        time.sleep(VIEW_SETTLE_SECONDS)
        payload: dict[str, Any] = {"op": "next"}
        if focused:
            payload["left"] = items_last(focused, here_name())
        ok = _ask_mac(payload)
    finally:
        restore_this_space(client)
    if ok:
        _toast(client, "Next waiting agent", "Next waiting agent")
    return ok


def _current_place(client: HerdrClient) -> dict[str, Any] | None:
    agent = _focused_agent(client)
    if not agent:
        return None
    found = dict(agent)
    found["machine"] = here_name()
    return found


def _focus_stack_target(client: HerdrClient, target: dict[str, Any]) -> bool:
    pane = target.get("pane_id") or ""
    on_server = _pane_on_server(client, pane)
    if on_server and (sys.platform != "darwin" or client_showing_local()):
        ok = _focus_local(client, target)
        _log(f"stack socket {target.get('title')} ok={ok}")
        return ok
    if sys.platform == "darwin":
        return focus_by_seq(client, pane, target.get("title") or "", target.get("machine") or "")
    return _ask_mac({
        "op": "focus",
        "pane_id": pane,
        "title": target.get("title") or "",
        "machine": target.get("machine") or "",
    })


def jump_back(client: HerdrClient) -> bool:
    """Focus the session you just left. That session stays available as redo."""
    items = _read_back()
    _log(f"prev start count={len(items)} top={(items[-1].get('title') if items else '')}")
    if not items:
        if sys.platform != "darwin":
            return _ask_mac({"op": "prev"})
        return False
    target = items[-1]
    on_server = _pane_on_server(client, target.get("pane_id") or "")
    # Older builds stored every machine as "Local". A pane that exists here is not the Mac one.
    if sys.platform != "darwin" and target.get("machine") == "Local" and on_server:
        items.pop()
        _write_back(items)
        return jump_back(client)
    left = _current_place(client)
    expect_navigation(target.get("pane_id") or "")
    if not _focus_stack_target(client, target):
        cancel_expect()
        _log(f"prev failed pane={target.get('pane_id')} machine={target.get('machine')}")
        return False
    items.pop()
    _write_back(items)
    if left and left.get("pane_id") != target.get("pane_id"):
        push_forward(left, left.get("machine") or here_name())
    _toast(client, target.get("title") or "", "Previous agent")
    return True


def jump_redo(client: HerdrClient) -> bool:
    """Option+n with nobody waiting: return to the session the last Option+p left."""
    items = _read_stack(FORWARD_FILE)
    if not items:
        _log("next: nobody waiting, no redo")
        return False
    target = items[-1]
    left = _current_place(client)
    expect_navigation(target.get("pane_id") or "")
    if not _focus_stack_target(client, target):
        cancel_expect()
        _log(f"redo failed pane={target.get('pane_id')}")
        return False
    items.pop()
    _write_stack(FORWARD_FILE, items)
    if left and left.get("pane_id") != target.get("pane_id"):
        push_back(left, left.get("machine") or here_name())
    _toast(client, target.get("title") or "", "Redo")
    _log(f"next: redo {target.get('title')}")
    return True


def _jump_waiting(client: HerdrClient) -> bool:
    """Land on the next agent that is waiting. False if there isn't one."""
    if sys.platform != "darwin":
        if focus_sibling(client):
            _log("next: sibling on this machine")
            return True
        ok = _handoff_next(client)
        _log(f"next: handoff to mac {ok}")
        return ok
    wezterm = _wezterm()
    pane_id = find_wezterm_pane(wezterm) if wezterm else None
    if not wezterm or pane_id is None:
        return focus_sibling(client)
    machine = header_machine(sidebar_text(_screen(wezterm, pane_id))).lower()
    if machine and machine != "local":
        if _jump_remote(client, machine):
            return True
        return keystroke_to_waiting(client, remember=False)
    if focus_sibling(client):
        return True
    return keystroke_to_waiting(client)


def jump_to_waiting(client: HerdrClient) -> bool:
    """Waiting agent wins. Otherwise return to the session you left, then redo that hop."""
    if _jump_waiting(client):
        clear_forward()
        _log("next: waiting, redo cleared")
        return True
    if jump_redo(client):
        return True
    _log("next: nobody waiting, back to the session you left")
    return jump_back(client)
