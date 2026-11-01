# agent-exporter

Metrics for a Linux box that runs coding agents, written as a Prometheus text file that
[Beszel](https://github.com/henrygd/beszel) can chart. It answers four questions from the menu bar
or a dashboard. How many agent sessions are running, and how much memory do they hold? Which ones
have gone stale? Is any session waiting for you? Did an agent leave a process burning a CPU core?

It reads `/proc`, the modification dates of Claude Code transcripts, small state files written by
Claude Code hooks, and ollama's local API. It never opens a transcript or reads its contents.
Python 3.10+, standard library only.

## Metrics

| Metric | Meaning |
|---|---|
| `agent_processes{agent}` | Running agent sessions. A wrapper and its native binary count once. |
| `agent_memory_bytes{agent}` | Resident memory of those processes. |
| `agent_stale_processes{agent}` | Sessions with no activity for `--stale-after` (default 24h). |
| `agent_stale_memory_bytes` | Memory those stale sessions hold. |
| `agent_sessions{state}` | Claude Code sessions that are `working`, `waiting` (needs you) or `idle`, from hooks. |
| `agent_waiting_longest_seconds` | How long the longest-waiting session has waited. |
| `agent_runaway_processes` | Processes an agent started that kept a core busy past `--runaway-after` (default 1h). |
| `agent_runaway_longest_seconds` | How long the worst one has been busy. |
| `ollama_up`, `ollama_models_loaded`, `ollama_memory_bytes`, `ollama_vram_bytes` | ollama state. |

Agents recognised: claude, codex, gemini, pi, opencode, aider, cursor-agent, goose, amp, qwen,
crush, copilot. `--agents` lists the ones always reported, even at zero.

How activity is judged:
1. Claude Code: the transcript's last write time and the latest hook event. The session id comes
   from the environment of a child process (an MCP server), and the file is only `stat()`-ed.
2. Other agents, or a Claude session with neither: CPU use between two runs above `--active-cpu`
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
uv tool install ~/src/agent-exporter
agent-exporter install-hooks              # adds async hooks to ~/.claude/settings.json, with a backup
cp systemd/agent-exporter.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now agent-exporter
```

The service writes `~/.local/state/agent-exporter/agents.prom` every 20 seconds. Hooks and CPU
samples live in `$XDG_RUNTIME_DIR/agent-exporter`. User services need lingering
(`loginctl enable-linger $USER`) to run without a login session.

## Beszel

Custom metrics need Beszel with [#2562](https://github.com/henrygd/beszel/pull/2562), which is not
merged yet. Copy `beszel/config.yml` into the Beszel agent's data directory, change `path`, and make
sure the agent's user can read the file and every directory above it.

`scripts/check-beszel.sh` runs Beszel's own parser and collector from that pull request on a fresh
file from this machine. It fails when Beszel rejects a line, a metric is not a gauge, a unit is
lost, or a series has no display name. Run it after changing the metrics.

## Commands

| Command | What it does |
|---|---|
| `agent-exporter show [--json]` | Print the current snapshot, with stale and runaway pids. Writes nothing. |
| `agent-exporter collect [--out PATH or -] [--json]` | Write the file once. |
| `agent-exporter run [--interval 20s]` | Write it on a loop. This is what the service runs. |
| `agent-exporter hook` | Called by Claude Code hooks. Reads the event from stdin, prints nothing, always exits 0. |
| `agent-exporter install-hooks` / `uninstall-hooks` `[--dry-run] [--json]` | Edit Claude Code settings. Only touches its own entries. |

Exit codes: 0 success, 1 the file could not be written, 2 bad arguments.

## For AI agents

- Use `agent-exporter show --json` to find stale sessions (`.stale[].pid`, `.idle_seconds`) and
  runaway processes (`.runaway[].pid`, `.cmd`). It is read-only.
- It reports. It never kills anything. Decide with the user before stopping a process: a stale
  Claude session may be one they want to resume.
- After changing metric names or labels, update `beszel/config.yml` and run
  `scripts/check-beszel.sh`. Beszel keeps 64 series per machine by default, so keep labels few.
- Tests: `uv run --with pytest pytest -q`. They run on a fake `/proc`.

## Decision record

Why this exists and what was ruled out: `docs/plans/2026-11-01-ai-box-agent-monitor.md`.
