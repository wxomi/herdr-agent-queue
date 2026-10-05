"""Core transition detection, queue lifecycle, and dispatch engine."""

from __future__ import annotations

import logging
from typing import Any

from agent_queue.client import HerdrClient
from agent_queue.state import QueueItem, QueueState

logger = logging.getLogger(__name__)


class QueueEngine:
    """Monitors agent status transitions across machines and handles dispatching."""

    def __init__(self, state: QueueState, client: HerdrClient):
        self.state = state
        self.client = client
        # Map of (machine, pane_id) -> status ("idle", "working", "blocked", "done")
        self.last_status: dict[tuple[str, str], str] = {}
        # Currently focused (machine, pane_id)
        self.focused_agent: tuple[str, str] | None = None

    def tick(self) -> None:
        """Single polling tick across Local and saved SSH machines."""
        self.state.load()
        machines = ["Local"] + self.client.list_machines()

        all_current: list[tuple[str, dict[str, Any]]] = []
        new_focused: tuple[str, str] | None = None

        for m in machines:
            agents = self.client.list_agents(m)
            for a in agents:
                pid = a.get("pane_id")
                if not pid:
                    continue
                all_current.append((m, a))
                if a.get("focused"):
                    new_focused = (m, pid)

        # Detect transitions
        for m, a in all_current:
            pid = a["pane_id"]
            curr_status = a.get("agent_status") or "unknown"
            key = (m, pid)
            prev_status = self.last_status.get(key)

            if prev_status is not None and prev_status != curr_status:
                # 1. Completion transition: was working -> now idle/blocked/done
                if prev_status == "working" and curr_status in ("idle", "blocked", "done"):
                    # Only queue if user is not actively focused on this pane
                    if self.focused_agent != key:
                        item = QueueItem(
                            machine=m,
                            pane_id=pid,
                            tab_id=a.get("tab_id", ""),
                            title=a.get("title") or a.get("display_agent", ""),
                        )
                        self.state.push(item)

                # 2. Reply sent transition: focused pane was idle/blocked/done -> now working
                if (
                    self.focused_agent == key
                    and prev_status in ("idle", "blocked", "done")
                    and curr_status == "working"
                ):
                    if self.state.auto_advance and self.state.queue:
                        self.advance_next(current_pane_id=pid)

            self.last_status[key] = curr_status

        # If user manually focused a pane, remove it from waiting queue
        if new_focused:
            self.state.remove(new_focused[1], machine=new_focused[0])

        self.focused_agent = new_focused

    def advance_next(self, current_pane_id: str | None = None) -> bool:
        """Jump to the next waiting agent in queue."""
        item = self.state.pop_next(current_pane_id=current_pane_id)
        if not item:
            self.client.show_toast(
                "Attention queue empty",
                body="All agents are up to date.",
                sound="none",
            )
            return False

        self.client.focus_agent(item.pane_id, tab_id=item.tab_id, machine=item.machine)
        return True

    def advance_prev(self, current_pane_id: str | None = None) -> bool:
        """Backtrack to the previously visited agent in queue history."""
        item = self.state.pop_prev(current_pane_id=current_pane_id)
        if not item:
            self.client.show_toast(
                "No previous agent",
                body="Queue history is empty.",
                sound="none",
            )
            return False

        self.client.focus_agent(item.pane_id, tab_id=item.tab_id, machine=item.machine)
        return True

    def toggle_auto(self) -> bool:
        """Toggle autopilot auto-advance on reply submission."""
        enabled = self.state.toggle_auto_advance()
        status_text = "ON (conveyor mode)" if enabled else "OFF (manual Option+Right)"
        body_text = (
            "Auto-advances when you submit a reply."
            if enabled
            else "Press Option+Right to jump to next agent."
        )
        self.client.show_toast(
            f"Agent Queue Autopilot: {status_text}",
            body=body_text,
            sound="none",
        )
        return enabled
