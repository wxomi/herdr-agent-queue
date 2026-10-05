"""Queue state model with atomic JSON persistence."""

from __future__ import annotations

import dataclasses
import json
import os
import time
from typing import Any

from agent_queue.config import DEFAULT_AUTO_ADVANCE, STATE_FILE


@dataclasses.dataclass
class QueueItem:
    machine: str
    pane_id: str
    tab_id: str = ""
    workspace_id: str = ""
    title: str = ""
    seq: int = 0
    finished_at: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueItem:
        return cls(
            machine=data.get("machine", "Local"),
            pane_id=data.get("pane_id", ""),
            tab_id=data.get("tab_id", ""),
            workspace_id=data.get("workspace_id", ""),
            title=data.get("title", ""),
            seq=data.get("seq", 0),
            finished_at=data.get("finished_at", time.time()),
        )


class QueueState:
    """Manages the FIFO attention queue and visited history with atomic file persistence."""

    def __init__(self, file_path: str = STATE_FILE):
        self.file_path = file_path
        self.auto_advance: bool = DEFAULT_AUTO_ADVANCE
        self.queue: list[QueueItem] = []
        self.history: list[QueueItem] = []
        self.load()

    def push(self, item: QueueItem, check_history: bool = False) -> bool:
        """Push a newly finished agent to queue. Return False if already queued or in history."""
        self.load()
        for existing in self.queue:
            if existing.machine == item.machine and existing.pane_id == item.pane_id:
                return False
        if check_history:
            for hist in self.history:
                if hist.machine == item.machine and hist.pane_id == item.pane_id:
                    return False
        self.queue.append(item)
        self.save()
        return True

    def remove_from_history(self, pane_id: str, machine: str = "Local") -> None:
        """Remove a pane from history when it transitions back to active/working."""
        self.load()
        orig_len = len(self.history)
        self.history = [
            h for h in self.history
            if not (h.pane_id == pane_id and (machine is None or h.machine == machine))
        ]
        if len(self.history) != orig_len:
            self.save()

    def pop_next(self, current_pane_id: str | None = None) -> QueueItem | None:
        """Pop the next waiting agent in FIFO order. Skips current_pane_id if at head."""
        self.load()
        if not self.queue:
            return None

        target_idx = 0
        if current_pane_id and len(self.queue) > 1 and self.queue[0].pane_id == current_pane_id:
            target_idx = 1

        item = self.queue.pop(target_idx)
        self.history.append(item)
        if len(self.history) > 30:
            self.history.pop(0)
        self.save()
        return item

    def pop_next_local(
        self,
        current_machine: str = "Local",
        current_pane_id: str | None = None,
        fallback_to_any: bool = True,
    ) -> QueueItem | None:
        """Backwards-compatible pop_next across waiting agents."""
        return self.pop_next(current_pane_id=current_pane_id)

    def pop_prev(self, current_pane_id: str | None = None) -> QueueItem | None:
        """Backtrack to the most recently visited agent in history."""
        self.load()
        if not self.history:
            return None

        target_idx = len(self.history) - 1
        if current_pane_id and self.history[target_idx].pane_id == current_pane_id:
            if len(self.history) < 2:
                return None
            target_idx = len(self.history) - 2

        item = self.history.pop(target_idx)
        self.save()
        return item

    def pop_prev_local(
        self,
        current_machine: str = "Local",
        current_pane_id: str | None = None,
        fallback_to_any: bool = True,
    ) -> QueueItem | None:
        """Backwards-compatible pop_prev across history."""
        return self.pop_prev(current_pane_id=current_pane_id)

    def remove(self, pane_id: str, machine: str | None = None) -> None:
        """Remove a pane from queue when user manually visits or closes it."""
        self.load()
        orig_len = len(self.queue)
        self.queue = [
            q for q in self.queue
            if not (q.pane_id == pane_id and (machine is None or q.machine == machine))
        ]
        if len(self.queue) != orig_len:
            self.save()

    def toggle_auto_advance(self) -> bool:
        """Toggle autopilot auto-advance on reply."""
        self.load()
        self.auto_advance = not self.auto_advance
        self.save()
        return self.auto_advance

    def load(self) -> None:
        """Load state from disk."""
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.auto_advance = bool(data.get("auto_advance", DEFAULT_AUTO_ADVANCE))
            self.queue = [QueueItem.from_dict(d) for d in data.get("queue", [])]
            self.history = [QueueItem.from_dict(d) for d in data.get("history", [])]
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        """Atomically persist state to disk."""
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        tmp_path = f"{self.file_path}.tmp.{os.getpid()}"
        data = {
            "auto_advance": self.auto_advance,
            "queue": [q.to_dict() for q in self.queue],
            "history": [h.to_dict() for h in self.history],
            "updated_at": time.time(),
        }
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.file_path)
        except OSError:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
