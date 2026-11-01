# agent-exporter

[![CI](https://github.com/evoleinik/agent-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/evoleinik/agent-exporter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Which of your coding agents need you right now? agent-exporter watches every Claude Code, Codex,
Gemini CLI and OpenCode session on a Linux box and sorts each one into exactly one status:
working, needs you (a permission, a question, a plan to approve), your turn (finished, waiting for
your next message) or stale. It writes those counts, plus memory, runaway child processes and
ollama, as a Prometheus text file that [Beszel](https://github.com/henrygd/beszel) can chart.

![The menu bar: live CPU and RAM per machine, working agents over the last hour, 1 session needs you, 18 are your turn](docs/menu-bar.png)

*The menu bar view comes from the [BeszelBar agents fork](https://github.com/evoleinik/BeszelBar/tree/agents),
drawn by its own code from sample data. N counts sessions that need you. T counts finished turns.
W graphs working sessions, with a cyan median per 16 minutes.*

It reads `/proc`, the modification dates of Claude Code transcripts, small state files written by
agent hooks, and ollama's local API. It never opens a transcript or reads its contents. Python
3.10+, standard library only.

## Why

Each agent vendor shows its own sessions. If you run dozens of sessions from several agents on
one remote box, nothing tells you which one is blocked on you, which finished an hour ago, and
which has sat untouched for a week holding a gigabyte of RAM. On the box this was built on, the
first run found 103 of 131 Claude sessions idle for over a day, holding about 20 GB, and a test
process an agent had left at 100% CPU for 3.5 days.

## Quick start

```bash
uv tool install --editable .        # or: pipx install .
agent-exporter install-hooks        # Claude Code; add --agent codex|gemini|opencode for the others
agent-exporter show                 # print the snapshot; writes nothing
```

## Metrics

| Metric | Meaning |
|---|---|
| `agent_processes{agent}` | Running agent sessions. A wrapper and its native binary count once. |
| `agent_memory_bytes{agent}` | Resident memory of those processes. |
| `agent_sessions{status}` | Sessions that are `working`, `needs_you`, `your_turn` or `stale` (no activity for `--stale-after`, default 24h). Each session is in exactly one. |
| `agent_stale_memory_bytes` | Memory the stale sessions hold. |
| `agent_needs_you_session_seconds{reason,session}` | How long each session that needs you has waited, and why: permission, question, plan, input or form. At most 5. |
| `agent_needs_you_longest_seconds` | The longest of those waits. |
| `agent_your_turn_session_seconds{session}` | How long each finished session has waited for your next message, oldest first. At most 5. |
| `agent_runaway_processes` | Processes an agent started that kept a core busy past `--runaway-after` (default 1h). |
| `agent_runaway_longest_seconds` | How long the worst one has been busy. |
| `ollama_up`, `ollama_models_loaded`, `ollama_memory_bytes`, `ollama_vram_bytes` | ollama state. |

Agents recognised: claude, codex, gemini, pi, opencode, aider, cursor-agent, goose, amp, qwen,
crush, copilot. `--agents` lists the ones always reported, even at zero.

How activity is judged:
1. Hooks, where installed: Claude Code, Codex, Gemini CLI and OpenCode tell the exporter when a
   session starts working, needs you (a permission, a question, a plan to approve) or finishes.
   Claude Code also gets its transcript's last write time and its own `~/.claude/sessions` record. The session id comes
   from the environment of a child process (an MCP server), and the file is only `stat()`-ed.
2. Other agents, or a session with no hook evidence: CPU use between two runs above `--active-cpu`
   (default 0.05 cores). Claude's CPU is not trusted when better evidence exists, because every
   session wakes up at once when `settings.json` changes.
3. With no evidence at all, a process is judged from when the exporter first saw it. So a codex
   session only becomes stale after the exporter has watched it idle for the full period.

Runs less than 5 seconds apart are not compared, because tick rounding fakes CPU rates. A busy
period ends only when CPU drops below half of `--runaway-cpu`, so a dip under load keeps it.

A runaway is a non-agent process that is a descendant of an agent, or that still carries an agent's
environment marker (`CLAUDECODE`, `AI_AGENT`, `PI_CODING_AGENT`, `CODEX_SANDBOX`, `GEMINI_CLI`)
after its parent died.

## Install

```bash
uv tool install --editable .   # editable: the service always runs this folder's code
agent-exporter install-hooks              # adds async hooks to ~/.claude/settings.json, with a backup
agent-exporter install-hooks --agent codex      # ~/.codex/hooks.json; then trust them once in codex /hooks
agent-exporter install-hooks --agent gemini     # ~/.gemini/settings.json
agent-exporter install-hooks --agent opencode   # ~/.config/opencode/plugins/agent-exporter.js
cp systemd/agent-exporter.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now agent-exporter
```

The service writes `~/.local/state/agent-exporter/agents.prom` every 20 seconds. Hooks and CPU
samples live in `$XDG_RUNTIME_DIR/agent-exporter`. User services need lingering
(`loginctl enable-linger $USER`) to run without a login session.

### Hooks per agent

| Agent | File | Events | Notes |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | SessionStart, UserPromptSubmit, PreToolUse (AskUserQuestion, ExitPlanMode), PermissionRequest, PostToolUse, Notification, Stop, SessionEnd | Async, so Claude never waits. |
| Codex | `~/.codex/hooks.json` | SessionStart, UserPromptSubmit, PermissionRequest, PostToolUse, Stop, SessionEnd | Codex skips async hooks, so these run in turn (about 45 ms each). A new or changed hook runs only after you trust it once in Codex's `/hooks` screen. |
| Gemini CLI | `~/.gemini/settings.json` | SessionStart, BeforeAgent, BeforeTool (ask_user, exit_plan_mode), AfterTool, Notification (ToolPermission), AfterAgent, SessionEnd | Timeouts are in milliseconds. Hooks are on by default. |
| OpenCode | `~/.config/opencode/plugins/agent-exporter.js` | session.status, session.idle, session.error, session.deleted, permission.asked and .replied, question.asked, .replied and .rejected | A plugin. It skips subagent sessions, and sends its own pid and a timestamp, because its helper processes can finish out of order or outlive OpenCode. |

Checked against Codex 0.145, Gemini CLI 0.52 and OpenCode 1.17 on 2026-11-01: a one-line prompt
through each produced the events above, tied to the right process. `uninstall-hooks --agent NAME`
removes ours and leaves the rest.

## Beszel

Custom metrics need Beszel with [#2562](https://github.com/henrygd/beszel/pull/2562), which is not
merged yet. Copy `beszel/config.yml` into the Beszel agent's data directory, change `path`, and make
sure the agent's user can read the file and every directory above it.

Example user units for a hub and an agent built from that pull request are in `beszel/systemd/`.
They read their login and keys from `~/.config/beszel/hub.env`, which you create with mode 600.
macOS apps refuse plain HTTP to an IP address, so put the hub behind HTTPS (for example
`tailscale serve`) before pointing BeszelBar at it.

The menu bar client is the [BeszelBar agents fork](https://github.com/evoleinik/BeszelBar/tree/agents).
It draws the N, T and W counts, colours each machine's RAM by memory pressure, and lists the
sessions that need you in its dropdown.

`scripts/check-beszel.sh` runs Beszel's own parser and collector from that pull request on a fresh
file from this machine. It fails when Beszel rejects a line, a metric is not a gauge, a unit is
lost, or a series has no display name. Run it after changing the metrics.

## Memory pressure

`scripts/mem-pressure.sh OUT.prom [seconds]` writes `memory_pressure_level` every 10 seconds:
0 normal, 1 warning, 2 critical. BeszelBar colours each machine's RAM area by it, green, yellow
or red, as Activity Monitor does. Used RAM alone says little, because it sits near full.

On Linux it reads PSI from `/proc/pressure/memory`. Warning means some task stalled on memory for
at least 10% of the last 10 seconds. Critical means every task stalled for at least 10% of it. On
macOS it reads the kernel's own level, `kern.memorystatus_vm_pressure_level`.

Box runs it as `systemd/mem-pressure.service`. A Mac runs `launchd/dev.beszel.mem-pressure.plist`
with the script copied to `~/.local/bin`. Add its file to the Beszel agent's `config.yml` as a
second source, as in `beszel/config.yml`. `tests/test_mem_pressure.py` covers both mappings.

## Commands

| Command | What it does |
|---|---|
| `agent-exporter show [--json]` | Print the current snapshot, with stale and runaway pids. Writes nothing. |
| `agent-exporter collect [--out PATH or -] [--json]` | Write the file once. |
| `agent-exporter run [--interval 20s]` | Write it on a loop. This is what the service runs. |
| `agent-exporter hook` | Called by agent hooks. Reads the event from stdin, prints nothing, always exits 0. |
| `agent-exporter install-hooks` / `uninstall-hooks` `[--agent NAME] [--dry-run] [--json]` | Edit an agent's hook file (Claude Code by default). Only touches its own entries. |

Exit codes: 0 success, 1 the file could not be written, 2 bad arguments.

## For AI agents

- Use `agent-exporter show --json` to find stale sessions (`.stale[].pid`, `.idle_seconds`) and
  runaway processes (`.runaway[].pid`, `.cmd`). It is read-only.
- It reports. It never kills anything. Decide with the user before stopping a process: a stale
  Claude session may be one they want to resume.
- After changing metric names or labels, update `beszel/config.yml` and run
  `scripts/check-beszel.sh`. Beszel keeps 64 series per machine by default, so keep labels few.
- Tests: `uv run --with pytest pytest -q`. They run on a fake `/proc`.

## Limitations

- Linux only for the process scan. On macOS only `scripts/mem-pressure.sh` runs.
- Charting needs Beszel with the unmerged custom-metrics pull request #2562.
- Agents without hooks (aider, Cursor CLI, Goose, Amp, pi and others) get working, your turn and
  stale from CPU use only. They never show "needs you".
- Hook event names follow each agent's current release and can change. The tests pin the names
  checked on 2026-11-01.

## License

MIT
