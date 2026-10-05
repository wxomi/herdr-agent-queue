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
        self._tick_count: int = 0
        self._remote_cache: dict[str, list[dict[str, Any]]] = {}

    def tick(self) -> None:
        """Single polling tick across Local and saved SSH machines."""
        self.state.load()
        self._tick_count += 1

        remote_machines = self.client.list_machines()
        poll_remote = (self._tick_count % 6 == 1) or not self._remote_cache

        all_current: list[tuple[str, dict[str, Any]]] = []
        new_focused: tuple[str, str] | None = None

        # 1. Local agents (always polled, fast <10ms)
        local_agents = self.client.list_agents("Local")
        for a in local_agents:
            pid = a.get("pane_id")
            if not pid:
                continue
            all_current.append(("Local", a))
            if a.get("focused"):
                new_focused = ("Local", pid)

        # 2. Remote agents (throttled every 6 ticks)
        for m in remote_machines:
            if poll_remote:
                agents = self.client.list_agents(m)
                self._remote_cache[m] = agents
            else:
                agents = self._remote_cache.get(m, [])

            for a in agents:
                pid = a.get("pane_id")
                if not pid:
                    continue
                all_current.append((m, a))
                if a.get("focused") and not new_focused:
                    new_focused = (m, pid)

        is_initial_tick = len(self.last_status) == 0

        # Detect transitions
        for m, a in all_current:
            pid = a["pane_id"]
            curr_status = a.get("agent_status") or "unknown"
            key = (m, pid)
            prev_status = self.last_status.get(key)

            if is_initial_tick:
                # Seed any existing waiting agent not currently focused
                if curr_status in ("idle", "blocked", "done") and new_focused != key:
                    item = QueueItem(
                        machine=m,
                        pane_id=pid,
                        tab_id=a.get("tab_id", ""),
                        workspace_id=a.get("workspace_id", ""),
                        title=a.get("title") or a.get("display_agent", ""),
                    )
                    self.state.push(item)
            elif prev_status is not None and prev_status != curr_status:
                # 1. Completion transition: was working -> now idle/blocked/done
                if prev_status == "working" and curr_status in ("idle", "blocked", "done"):
                    # Only queue if user is not actively focused on this pane
                    if self.focused_agent != key:
                        item = QueueItem(
                            machine=m,
                            pane_id=pid,
                            tab_id=a.get("tab_id", ""),
                            workspace_id=a.get("workspace_id", ""),
                            title=a.get("title") or a.get("display_agent", ""),
                        )
                        self.state.push(item)

                # 2. Reply sent transition: focused pane was idle/blocked/done -> now working
                if (
                    self.focused_agent == key
                    and prev_status in ("idle", "blocked", "done")
                    and curr_status == "working"
                ):
                    if self.state.auto_advance:
                        self.advance_next(current_pane_id=pid)

            self.last_status[key] = curr_status

        # If user manually focused a pane, remove it from waiting queue
        if new_focused:
            self.state.remove(new_focused[1], machine=new_focused[0])

        self.focused_agent = new_focused

    def advance_next(self, current_pane_id: str | None = None) -> bool:
        """Jump to the next waiting agent in queue, or fallback to cycling waiting agents."""
        item = self.state.pop_next(current_pane_id=current_pane_id)
        if item:
            self.client.focus_agent(
                item.pane_id,
                tab_id=item.tab_id,
                workspace_id=item.workspace_id,
                machine=item.machine,
            )
            rem = len(self.state.queue)
            rem_text = f"{rem} left in queue" if rem > 0 else "Queue now empty"
            self.client.show_toast(
                f"Next: {item.title or item.pane_id}",
                body=f"[{item.machine}] {rem_text}",
                sound="none",
            )
            return True

        # Fallback: Attention queue is empty. Cycle through waiting agents.
        return self._cycle_agents(direction=1, current_pane_id=current_pane_id)

    def advance_prev(self, current_pane_id: str | None = None) -> bool:
        """Backtrack to the previously visited agent in queue history, or fallback to reverse cycling."""
        item = self.state.pop_prev(current_pane_id=current_pane_id)
        if item:
            self.client.focus_agent(
                item.pane_id,
                tab_id=item.tab_id,
                workspace_id=item.workspace_id,
                machine=item.machine,
            )
            self.client.show_toast(
                f"Backtrack: {item.title or item.pane_id}",
                body=f"[{item.machine}] Returned to previous agent",
                sound="none",
            )
            return True

        # Fallback: History is empty. Cycle backwards through waiting agents.
        return self._cycle_agents(direction=-1, current_pane_id=current_pane_id)

    def _cycle_agents(self, direction: int, current_pane_id: str | None = None) -> bool:
        """Cycle through waiting agents when queue/history is empty."""
        local_agents = self.client.list_agents("Local")
        if not local_agents:
            self.client.show_toast(
                "No agents found",
                body="No running agents detected on this machine.",
                sound="none",
            )
            return False

        # Identify currently focused pane if not explicitly given
        current_pid = current_pane_id
        if not current_pid:
            for a in local_agents:
                if a.get("focused"):
                    current_pid = a.get("pane_id")
                    break

        # Prioritize agents waiting for input (idle, blocked, done)
        waiting = [
            a for a in local_agents
            if a.get("agent_status") in ("idle", "blocked", "done")
        ]
        pool = waiting if waiting else local_agents

        # If only 1 agent in pool and it's already current
        if len(pool) == 1 and pool[0].get("pane_id") == current_pid:
            self.client.show_toast(
                "Only 1 agent available",
                body=pool[0].get("title") or pool[0].get("pane_id", ""),
                sound="none",
            )
            return False

        # Find current index in pool
        curr_idx = -1
        for idx, a in enumerate(pool):
            if a.get("pane_id") == current_pid:
                curr_idx = idx
                break

        if curr_idx == -1:
            target_idx = 0 if direction > 0 else len(pool) - 1
        else:
            target_idx = (curr_idx + direction) % len(pool)
            if len(pool) > 1 and pool[target_idx].get("pane_id") == current_pid:
                target_idx = (target_idx + direction) % len(pool)

        target = pool[target_idx]
        target_pid = target.get("pane_id", "")
        target_tab = target.get("tab_id", "")
        target_ws = target.get("workspace_id", "")
        target_title = target.get("title") or target.get("display_agent", "")
        status_desc = target.get("agent_status") or "active"

        self.client.focus_agent(
            target_pid,
            tab_id=target_tab,
            workspace_id=target_ws,
            machine="Local",
        )

        hist_item = QueueItem(
            machine="Local",
            pane_id=target_pid,
            tab_id=target_tab,
            workspace_id=target_ws,
            title=target_title,
        )
        self.state.history.append(hist_item)
        if len(self.state.history) > 30:
            self.state.history.pop(0)
        self.state.save()

        action_name = "Next" if direction > 0 else "Prev"
        self.client.show_toast(
            f"{action_name}: {target_title or target_pid}",
            body=f"[{status_desc}] (queue empty: cycling waiting agents)",
            sound="none",
        )
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
