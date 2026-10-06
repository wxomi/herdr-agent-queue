"""Core transition detection, queue lifecycle, and dispatch engine."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from agent_queue.client import HerdrClient
from agent_queue.native_jump import (
    client_typing_fingerprint,
    jump_back,
    jump_to_waiting,
    clear_forward,
    open_finished,
    remember_back,
    screen_machine,
    take_expected,
)
from agent_queue.state import QueueItem, QueueState

logger = logging.getLogger(__name__)

# herdr's effective status: "done" means finished and not yet seen, "idle" means seen.
WAITING = ("done", "blocked")
# A pause shorter than this is still "in the middle of typing."
TYPING_QUIET_SECONDS = 12


class QueueEngine:
    """Monitors agent status transitions across machines and handles dispatching."""

    def __init__(self, state: QueueState, client: HerdrClient, jump_native=None):
        self.state = state
        self.client = client
        self.jump_native = jump_native if jump_native is not None else jump_to_waiting
        # Map of (machine, pane_id) -> status ("idle", "working", "blocked", "done")
        self.last_status: dict[tuple[str, str], str] = {}
        # Currently focused (machine, pane_id)
        self.focused_agent: tuple[str, str] | None = None
        self._tick_count: int = 0
        self._remote_cache: dict[str, list[dict[str, Any]]] = {}
        self._remote_thread: threading.Thread | None = None
        self._pending_finish: tuple[str, dict[str, Any]] | None = None
        self._typed_fp: str | None = None
        self._typed_at: float = 0.0
        self._shown_snapshot: dict[str, Any] | None = None

    def tick(self) -> bool:
        """Single polling tick across Local (<0.3ms socket) and saved SSH machines (non-blocking)."""
        self.state.load()
        self._tick_count += 1
        if self.state.auto_advance:
            self._note_typing()

        remote_machines = self.client.list_machines()
        poll_remote = (self._tick_count % 6 == 1) or not self._remote_cache

        # Dispatch background fetch for remote machines to never stall local socket ticks
        if remote_machines and poll_remote and (self._remote_thread is None or not self._remote_thread.is_alive()):
            def _fetch_remote(machines: list[str]) -> None:
                for m in machines:
                    try:
                        agents = self.client.list_agents(m)
                        self._remote_cache[m] = agents
                    except Exception:
                        pass

            self._remote_thread = threading.Thread(
                target=_fetch_remote,
                args=(list(remote_machines),),
                daemon=True,
            )
            self._remote_thread.start()

        all_current: list[tuple[str, dict[str, Any]]] = []
        new_focused: tuple[str, str] | None = None

        # 1. Local agents (always polled over direct socket in <0.3ms)
        local_agents = self.client.list_agents("Local")
        for a in local_agents:
            pid = a.get("pane_id")
            if not pid:
                continue
            all_current.append(("Local", a))
            if a.get("focused"):
                new_focused = ("Local", pid)

        # 2. Remote agents (read immediately from non-blocking cache)
        for m in remote_machines:
            agents = self._remote_cache.get(m, [])
            for a in agents:
                pid = a.get("pane_id")
                if not pid:
                    continue
                all_current.append((m, a))
                if a.get("focused") and not new_focused:
                    new_focused = (m, pid)
        to_seed: list[QueueItem] = []

        # Detect transitions
        for m, a in all_current:
            pid = a["pane_id"]
            curr_status = a.get("agent_status") or "unknown"
            key = (m, pid)
            prev_status = self.last_status.get(key)
            seq = int(a.get("state_change_seq") or 0)

            if prev_status is None:
                # Seed any existing waiting agent not currently focused (initial tick or newly discovered machine)
                if curr_status in WAITING and new_focused != key:
                    item = QueueItem(
                        machine=m,
                        pane_id=pid,
                        tab_id=a.get("tab_id", ""),
                        workspace_id=a.get("workspace_id", ""),
                        title=a.get("title") or a.get("display_agent", ""),
                        seq=seq,
                    )
                    to_seed.append(item)
            elif prev_status != curr_status:
                # 1. Completion transition: was working -> now idle/blocked/done
                if prev_status not in WAITING and curr_status in WAITING:
                    # Only queue if user is not actively focused on this pane
                    if self.focused_agent != key:
                        self.state.remove_from_history(pid, machine=m)
                        item = QueueItem(
                            machine=m,
                            pane_id=pid,
                            tab_id=a.get("tab_id", ""),
                            workspace_id=a.get("workspace_id", ""),
                            title=a.get("title") or a.get("display_agent", ""),
                            seq=seq,
                        )
                        self.state.push(item, check_history=False)

                # 2. Reply sent transition: focused pane was idle/blocked/done -> now working
                if (
                    self.focused_agent == key
                    and prev_status in ("idle", "blocked", "done")
                    and curr_status == "working"
                ):
                    if self.state.auto_advance:
                        self.advance_next(current_pane_id=pid)

                # Remember a finish. Open it only once the user has stopped typing.
                if (
                    self.state.auto_advance
                    and prev_status == "working"
                    and curr_status in ("idle", "done", "blocked")
                    and self.focused_agent != key
                    and self._pending_finish is None
                ):
                    self._pending_finish = (m, a)

                if (
                    self.focused_agent == key
                    and curr_status == "working"
                    and self.state.auto_advance
                ):
                    self._pending_finish = None

            self.last_status[key] = curr_status

        self._open_pending(all_current)

        # Push seeded items: sort by state_change_seq descending (most recent first)
        # and do not re-seed items already in visited history
        if to_seed:
            to_seed.sort(key=lambda x: x.seq, reverse=True)
            for item in to_seed:
                self.state.push(item, check_history=True)

        # Drop queued agents that are no longer waiting (seen, working again, or gone).
        # Only judge machines we actually have data for this tick.
        seen_machines = {"Local"} | {m for m in remote_machines if m in self._remote_cache}
        current_status = {(m, a["pane_id"]): a.get("agent_status") for m, a in all_current}
        for q in list(self.state.queue):
            if q.machine in seen_machines and current_status.get((q.machine, q.pane_id)) not in WAITING:
                self.state.remove(q.pane_id, machine=q.machine)

        # If user manually focused a pane, remove it from waiting queue
        if new_focused:
            self.state.remove(new_focused[1], machine=new_focused[0])

        self.focused_agent = new_focused
        self._remember_switch(all_current)

        # Return True if active agents are working or queued (requires fast polling)
        has_active = any(a.get("agent_status") == "working" for _, a in all_current) or bool(self.state.queue)
        return has_active

    def _remember_switch(self, all_current: list[tuple[str, dict[str, Any]]]) -> None:
        """Option+p returns to the session a manual switch just left."""
        shown = self._shown_agent(all_current)
        if not shown or not shown.get("pane_id"):
            return
        if take_expected(shown["pane_id"]):
            self._shown_snapshot = shown
            return
        prev = self._shown_snapshot
        self._shown_snapshot = shown
        if not prev or prev.get("pane_id") == shown.get("pane_id"):
            return
        remember_back(prev, prev.get("machine") or "Local")
        clear_forward()

    def _note_typing(self) -> None:
        fp = client_typing_fingerprint()
        if fp != self._typed_fp:
            self._typed_fp = fp
            self._typed_at = time.monotonic()

    def _typing_quiet(self) -> bool:
        if self._typed_fp is None:
            return False
        return time.monotonic() - self._typed_at >= TYPING_QUIET_SECONDS

    def _open_pending(self, all_current: list[tuple[str, dict[str, Any]]]) -> None:
        pending = self._pending_finish
        if not pending:
            return
        machine, agent = pending
        pane = agent.get("pane_id")
        status = next(
            (a.get("agent_status") for m, a in all_current if m == machine and a.get("pane_id") == pane),
            None,
        )
        if status not in ("idle", "done", "blocked"):
            self._pending_finish = None
            return
        if not self._parked(all_current):
            return
        shown = self._shown_agent(all_current)
        if shown and shown.get("pane_id") == pane:
            self._pending_finish = None
            return
        if open_finished(self.client, machine, agent, shown):
            self._pending_finish = None

    def _parked(self, all_current: list[tuple[str, dict[str, Any]]]) -> bool:
        """True when the session on screen is idle and the user has stopped typing."""
        if not self._typing_quiet():
            return False
        showing = (screen_machine() or "local").lower()
        if showing in ("", "local"):
            for machine, agent in all_current:
                if machine == "Local" and agent.get("focused"):
                    return agent.get("agent_status") == "idle"
            return False
        for machine, agent in all_current:
            if machine.lower() == showing and agent.get("focused"):
                return agent.get("agent_status") == "idle"
        return False

    def _shown_agent(self, all_current: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
        showing = (screen_machine() or "local").lower()
        want = "Local" if showing in ("", "local") else showing
        for machine, agent in all_current:
            if agent.get("focused") and (machine == want or machine.lower() == want):
                return {**agent, "machine": machine}
        return None

    def _remaining_text(self) -> str:
        local_rem = sum(1 for q in self.state.queue if q.machine == "Local")
        remote_rem = len(self.state.queue) - local_rem
        parts = []
        if local_rem:
            parts.append(f"{local_rem} local left")
        if remote_rem:
            parts.append(f"{remote_rem} on remote")
        return " • ".join(parts) if parts else "Queue now empty"

    def _go(self, item: QueueItem, label: str) -> None:
        """Focus a queue item. Plugins cannot switch the client to another machine,
        so remote items are pre-focused in the background and announced instead."""
        auto_badge = "⚡ Auto: ON" if self.state.auto_advance else "⏸ Auto: OFF"
        title = item.title or item.pane_id
        if item.machine == "Local":
            self.client.focus_agent(
                item.pane_id,
                tab_id=item.tab_id,
                workspace_id=item.workspace_id,
                machine=item.machine,
            )
            self.client.show_toast(
                f"{label}: {title}",
                body=f"{self._remaining_text()} | {auto_badge}",
                sound="none",
                position="top-right",
            )
            return

        self.client.prefocus_remote_async(
            item.machine,
            item.pane_id,
            tab_id=item.tab_id,
            workspace_id=item.workspace_id,
        )
        self.client.show_toast(
            f"On {item.machine}: {title}",
            body=f"Switch to {item.machine} (prefix+w), it's focused there • {self._remaining_text()}",
            sound="none",
            position="top-right",
        )

    def advance_next(self, current_pane_id: str | None = None) -> bool:
        """Go to the next agent waiting on the user, on any machine."""
        # Native navigation first: it uses herdr's own unseen tracking, which the sidebar
        # dots follow and agent.list does not always reflect.
        if self.jump_native(self.client):
            return True

        item = self.state.pop_next(current_pane_id=current_pane_id)
        if item:
            self._go(item, "Next")
            return True
        self._nothing("No agents waiting on you")
        return False

    def advance_prev(self, current_pane_id: str | None = None) -> bool:
        """Go back to the agent Option+n just left."""
        if jump_back(self.client):
            return True
        item = self.state.pop_prev(current_pane_id=current_pane_id)
        if item:
            self._go(item, "Back")
            return True
        self._nothing("No earlier agent in history")
        return False

    def _nothing(self, title: str) -> None:
        self.client.show_toast(title, body=self._remaining_text(), sound="none", position="top-right")

    def toggle_auto(self) -> bool:
        """Toggle autopilot auto-advance on reply submission."""
        enabled = self.state.toggle_auto_advance()
        status_tag = "⚡ Autopilot: ON" if enabled else "⏸ Autopilot: OFF"
        body_text = (
            "Jumps when you send a reply, and when another agent finishes while you sit in an idle session."
            if enabled
            else "Manual mode: press Option+n (⌥n) to advance."
        )
        sound = "done" if enabled else "request"

        # 1. Herdr in-app toast with audible chime
        self.client.show_toast(
            status_tag,
            body=body_text,
            sound=sound,
            position="top-right",
        )

        # 2. OS-level native notification banner
        self.client.show_system_notification(
            title="Herdr Agent Queue",
            message=body_text,
            subtitle=status_tag,
        )
        return enabled

    def show_status(self) -> dict[str, Any]:
        """Display status notification and return current state info."""
        self.state.load()
        auto_badge = "⚡ Autopilot: ON" if self.state.auto_advance else "⏸ Autopilot: OFF"
        q_count = len(self.state.queue)
        top_info = f"Next: {self.state.queue[0].title}" if q_count > 0 else "All agents handled"
        body_text = f"{q_count} waiting agent{'s' if q_count != 1 else ''} • {top_info}"

        self.client.show_toast(
            auto_badge,
            body=body_text,
            sound="none",
            position="top-right",
        )
        return {
            "auto_advance": self.state.auto_advance,
            "queue_count": q_count,
            "queue": self.state.queue,
            "history": self.state.history,
        }
