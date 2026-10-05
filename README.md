# Herdr Agent Attention Queue

A hybrid keyboard-reflex and autopilot attention queue for AI agents across Herdr workspaces and machines.

## Why this exists

When running multiple AI coding agents (Kiro, Devin, Cursor, Agy, Claude) simultaneously across workspaces, developers waste time searching sidebars or cycling through panes to find which agent finished its turn and is waiting for input.

Existing solutions either cycle through all idle shells indiscriminately or force you into a heavy modal popup.

`herdr-agent-queue` solves this with a real attention queue:
- **Intelligent Queueing**: Only agents that actively transitioned `working -> idle` / `blocked` / `done` enter the queue. Untouched shells are never queued.
- **Cross-Machine Synchronization**: Unifies agents running locally on your laptop with remote agents on connected SSH machines (`notebook` / `vpn-server`).
- **Hybrid Controls**:
  - **Reflex Key (Default)**: Tap `Alt+Right` to warp to the next waiting agent whenever you are ready. Tap `Alt+Left` to go back.
  - **Autopilot Toggle (`prefix+y`)**: When enabled, submitting a reply automatically jumps to the next waiting agent the moment your current agent enters `working` state.

## Installation

```bash
herdr plugin link ~/.local/share/herdr-agent-queue
```

## Keybindings (`~/.config/herdr/config.toml`)

```toml
[[keys.command]]
key = "option+right"
type = "plugin_action"
command = "wxomi.agent-queue.next"
description = "jump to next agent waiting in attention queue"

[[keys.command]]
key = "option+left"
type = "plugin_action"
command = "wxomi.agent-queue.prev"
description = "jump to previous agent in queue history"

[[keys.command]]
key = "prefix+y"
type = "plugin_action"
command = "wxomi.agent-queue.toggle"
description = "toggle autopilot auto-advance on reply"
```
