"""Background daemon management and polling loop."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time

from agent_queue.client import HerdrClient
from agent_queue.config import IDLE_POLL_INTERVAL, LOCK_FILE, POLL_INTERVAL, STATE_DIR, STATE_FILE
from agent_queue.engine import QueueEngine
from agent_queue.state import QueueState


def acquire_lock() -> object | None:
    """Acquire exclusive flock on LOCK_FILE containing current PID."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        f = open(LOCK_FILE, "a+", encoding="utf-8")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.seek(0)
            f.truncate()
            f.write(f"{os.getpid()}\n")
            f.flush()
            return f
        except (BlockingIOError, OSError):
            f.close()
            return None
    except OSError:
        return None


def get_daemon_status() -> dict:
    """Check if daemon is currently active."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        f = open(LOCK_FILE, "a+", encoding="utf-8")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()
            return {"running": False, "pid": None}
        except (BlockingIOError, OSError):
            f.seek(0)
            pid_str = f.read().strip()
            f.close()
            try:
                pid = int(pid_str)
            except ValueError:
                pid = None
            return {"running": True, "pid": pid}
    except OSError:
        return {"running": False, "pid": None}


def stop_daemon() -> bool:
    """Stop running background daemon process."""
    status = get_daemon_status()
    if status["running"] and status["pid"]:
        try:
            os.kill(status["pid"], signal.SIGTERM)
            time.sleep(0.3)
        except OSError:
            pass
    return True


def start_daemon() -> int:
    """Launch daemon detached in background."""
    status = get_daemon_status()
    if status["running"]:
        return 0
    cmd = [sys.executable, "-m", "agent_queue", "--watch"]
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    return 0


def run_daemon() -> int:
    """Run continuous polling loop until interrupted or Herdr server restarts/exits."""
    lock = acquire_lock()
    if lock is None:
        return 0

    state = QueueState(STATE_FILE)
    client = HerdrClient()
    engine = QueueEngine(state, client)

    initial_server = client.server_identity()
    consecutive_misses = 0

    try:
        while True:
            # Check Herdr socket lifecycle
            curr_server = client.server_identity()
            if curr_server is not None:
                consecutive_misses = 0
                if initial_server is None:
                    initial_server = curr_server
                elif curr_server != initial_server:
                    # Socket inode changed: Herdr restarted or successor launched
                    break
            else:
                consecutive_misses += 1
                if consecutive_misses >= 10:
                    # Socket absent for > 5-15s: clean exit
                    break

            has_active = engine.tick()
            sleep_duration = POLL_INTERVAL if has_active else IDLE_POLL_INTERVAL
            time.sleep(sleep_duration)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass
    return 0
