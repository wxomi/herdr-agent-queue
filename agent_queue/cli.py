"""Command line interface entrypoint for herdr-agent-queue."""

from __future__ import annotations

import sys
import time

from agent_queue.client import HerdrClient
from agent_queue.config import STATE_FILE
from agent_queue.daemon import get_daemon_status, run_daemon, start_daemon, stop_daemon
from agent_queue.engine import QueueEngine
from agent_queue.state import QueueState


def main(argv: list[str] | None = None) -> int:
    """CLI dispatcher for daemon control and plugin actions."""
    args = argv[1:] if argv is not None else sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        print(
            "Usage: python3 -m agent_queue [COMMAND]\n\n"
            "Commands:\n"
            "  next        Jump to next agent in attention queue\n"
            "  prev        Jump to previous agent in queue history\n"
            "  toggle      Toggle autopilot auto-advance on reply\n"
            "  status      Print current queue state and daemon status\n"
            "  --watch     Run background polling daemon in foreground\n"
            "  --start     Start background polling daemon\n"
            "  --stop      Stop background polling daemon\n"
            "  --restart   Restart background polling daemon"
        )
        return 0

    cmd = args[0].lower()

    if cmd == "--watch":
        return run_daemon()

    if cmd == "--start":
        start_daemon()
        print("Agent queue daemon started in background.")
        return 0

    if cmd == "--stop":
        stop_daemon()
        print("Agent queue daemon stopped.")
        return 0

    if cmd == "--restart":
        stop_daemon()
        time.sleep(0.3)
        start_daemon()
        print("Agent queue daemon restarted.")
        return 0

    # Actions that require engine
    state = QueueState(STATE_FILE)
    client = HerdrClient()
    engine = QueueEngine(state, client)

    if cmd == "next":
        engine.advance_next()
        return 0

    if cmd == "prev":
        engine.advance_prev()
        return 0

    if cmd == "toggle":
        engine.toggle_auto()
        return 0

    if cmd == "status":
        daemon_status = get_daemon_status()
        state.load()
        status_line = (
            f"Daemon: {'RUNNING (PID ' + str(daemon_status['pid']) + ')' if daemon_status['running'] else 'STOPPED'}"
        )
        print(status_line)
        print(f"Autopilot: {'ENABLED (auto-advances on reply)' if state.auto_advance else 'DISABLED (manual Alt+Right)'}")
        print(f"Queue count: {len(state.queue)}")
        for idx, item in enumerate(state.queue, 1):
            print(f"  {idx}. [{item.machine}] {item.pane_id}: {item.title or 'Untitled'}")
        if state.history:
            print(f"Recent history ({len(state.history)}):")
            for idx, item in enumerate(reversed(state.history[-5:]), 1):
                print(f"  -{idx}. [{item.machine}] {item.pane_id}: {item.title or 'Untitled'}")
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
