#!/usr/bin/env python3
"""agent-exporter: AI agent metrics for a Linux box, written as a Prometheus text file.

It reads /proc, the modification dates of Claude Code transcripts (never their contents),
small state files that agent hooks write (Claude Code, Codex, Gemini CLI, OpenCode), and
ollama's local API. It writes a few
gauges that Beszel's custom metrics (henrygd/beszel#2562) can chart.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.1.0"

KNOWN_AGENTS = ("claude", "codex", "gemini", "pi", "opencode", "aider", "cursor-agent",
                "goose", "amp", "qwen", "crush", "copilot")
DEFAULT_AGENTS = ("claude", "codex")
INTERPRETERS = ("node", "bun", "deno", "python", "python3")
PACKAGE_HINTS = (("/@anthropic-ai/claude-code/", "claude"), ("/@openai/codex/", "codex"),
                 ("/@google/gemini-cli/", "gemini"))
# Native Claude Code builds: the terminal installer and the desktop app's remote CLI.
CLAUDE_PATHS = ("/claude/versions/", "/ccd-cli/")
# Environment keys that agents set in every process they start. An orphaned child keeps them.
AGENT_ENV_MARKERS = ("CLAUDECODE", "AI_AGENT", "PI_CODING_AGENT", "CODEX_SANDBOX",
                     "CODEX_SANDBOX_NETWORK_DISABLED", "GEMINI_CLI")
STATES = ("working", "waiting", "idle")  # what a hook file records
# What a session is, from the person's side. Every session is in exactly one.
STATUSES = ("working", "needs_you", "your_turn", "stale")
REASONS = ("permission", "question", "plan", "input", "form")
NOTIFICATION_REASONS = {"permission_prompt": "permission", "agent_needs_input": "input",
                        "elicitation_dialog": "form", "elicitation_url_dialog": "form",
                        "ToolPermission": "permission"}  # the last one is Gemini CLI's
# Tools that stop and wait on the person: Claude Code's, then Gemini CLI's.
WAITING_TOOLS = {"AskUserQuestion": "question", "ExitPlanMode": "plan",
                 "ask_user": "question", "exit_plan_mode": "plan"}
# Claude Code hook events to install, with the matcher each needs (None = every occurrence).
HOOK_EVENTS = {
    "SessionStart": None, "UserPromptSubmit": None,
    "PreToolUse": "AskUserQuestion|ExitPlanMode",  # these two tools wait on the person
    "PermissionRequest": None, "PostToolUse": None, "PostToolUseFailure": None,
    "Notification": None, "Stop": None, "StopFailure": None, "SessionEnd": None,
}
# The other agents' hook files. Codex skips async hooks, so its hooks run in turn (they finish in
# well under a second); Gemini CLI counts timeouts in milliseconds. OpenCode takes a plugin instead.
CODEX_HOOK_EVENTS = {"SessionStart": None, "UserPromptSubmit": None, "PermissionRequest": None,
                     "PostToolUse": None, "Stop": None, "SessionEnd": None}
GEMINI_HOOK_EVENTS = {"SessionStart": None, "BeforeAgent": None, "BeforeTool": "ask_user|exit_plan_mode",
                      "AfterTool": None, "Notification": None, "AfterAgent": None, "SessionEnd": None}
HOOK_AGENTS = ("claude", "codex", "gemini", "opencode")
HOOK_MARK = re.compile(r"agent[-_]exporter(\.py)?['\"]? hook\b")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
WORKING_TIMEOUT = 1800  # a "working" session with no sign of life this long was interrupted
RECENT = 60  # seconds; activity this fresh means a session without hooks is working
MAX_WAITING_SERIES = 5  # one series per waiting session; Beszel keeps 64 series per machine
TRANSCRIPT_RETRY = 600  # seconds before searching again for a transcript that was not found
MIN_SAMPLE = 5.0  # seconds; a shorter gap between runs turns tick rounding into fake CPU rates


# ---------- paths ----------

def state_home() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "agent-exporter")


def runtime_home() -> str:
    # Hooks and the service must agree on this folder even when a session was started without
    # XDG_RUNTIME_DIR (from cron, for example), so fall back to the standard per-user path.
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(base, "agent-exporter") if os.path.isdir(base) else state_home()


def claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def hook_file(agent: str) -> str:
    """The file install-hooks edits for an agent."""
    if agent == "codex":
        return os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "hooks.json")
    if agent == "gemini":
        return os.path.expanduser("~/.gemini/settings.json")
    if agent == "opencode":
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "opencode", "plugins", "agent-exporter.js")
    return os.path.join(claude_home(), "settings.json")


def parse_duration(text: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    t = text.strip().lower()
    try:
        value = float(t[:-1]) * units[t[-1]] if t and t[-1] in units else float(t)
    except (ValueError, KeyError):
        raise argparse.ArgumentTypeError(f"bad duration {text!r}; use e.g. 90s, 30m, 24h, 2d")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"duration must be above zero: {text!r}")
    return value


def parse_agents(text: str) -> tuple[str, ...]:
    names = tuple(a for a in text.split(",") if a)
    bad = [a for a in names if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", a)]
    if bad:
        raise argparse.ArgumentTypeError(f"bad agent name {bad[0]!r}; use lowercase names like claude,codex")
    return names


def load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomic(path: str, text: str) -> None:
    """Write via a temp name that does not end in .prom, then rename (Beszel #2562 contract)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------- processes ----------

class Proc:
    __slots__ = ("pid", "ppid", "start_ticks", "start", "ticks", "rss", "argv", "exe", "agent",
                 "first_seen", "last_active", "busy_since")

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.start_ticks}"


def agent_name(argv: list[str], exe: str) -> str | None:
    """Name the coding agent a process is, or None. Helpers and wrappers are not agents.

    Judged by argv[0], never by exe: Claude Code runs its own binary as grep, find and rg
    (argv[0] ugrep, bfs, rg), and those must count as ordinary child processes.
    """
    if not argv:
        return None
    if any(mark in argv[0] for mark in CLAUDE_PATHS):
        return "claude"
    base = os.path.basename(argv[0])
    if base in KNOWN_AGENTS:
        return base
    if base in INTERPRETERS or re.fullmatch(r"(node|python3?)[\d.]*", base):
        for arg in argv[1:]:
            if arg.startswith("-"):
                continue
            for hint, name in PACKAGE_HINTS:
                if hint in arg:
                    return name
            stem = re.sub(r"\.(js|mjs|cjs|py)$", "", os.path.basename(arg))
            return stem if stem in KNOWN_AGENTS else None
    return None


def boot_time(proc: str) -> int:
    with open(os.path.join(proc, "stat")) as f:
        for line in f:
            if line.startswith("btime "):
                return int(line.split()[1])
    raise RuntimeError(f"no btime line in {proc}/stat")


def read_procs(proc: str, uid: int | None) -> tuple[dict[int, Proc], int]:
    clk = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    btime = boot_time(proc)
    out: dict[int, Proc] = {}
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        d = os.path.join(proc, name)
        try:
            if uid is not None and os.stat(d).st_uid != uid:
                continue
            with open(os.path.join(d, "stat")) as f:
                stat = f.read()
            with open(os.path.join(d, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue  # exited while we looked
        fields = stat[stat.rfind(")") + 2:].split()
        try:
            exe = os.readlink(os.path.join(d, "exe"))
        except OSError:
            exe = ""
        p = Proc()
        p.pid = int(name)
        p.ppid = int(fields[1])
        p.ticks = int(fields[11]) + int(fields[12])
        p.start_ticks = int(fields[19])
        p.start = btime + p.start_ticks / clk
        p.rss = int(fields[21]) * page
        p.argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        p.exe = exe
        p.agent = agent_name(p.argv, exe)
        out[p.pid] = p
    return out, clk


def read_env(proc: str, pid: int) -> dict[str, str]:
    try:
        with open(os.path.join(proc, str(pid), "environ"), "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def short_cmd(argv: list[str], limit: int = 100) -> str:
    text = " ".join(argv)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------- CPU tracking across runs ----------

def track_cpu(procs: dict[int, Proc], clk: int, prev: dict, now: float,
              active_cpu: float, busy_cpu: float) -> dict:
    """Update each process's activity and busy period from the previous run's tick counts."""
    prev_t = prev.get("t")
    prev_procs = prev.get("procs") or {}
    dt = now - prev_t if prev_t else None
    tracked = {}
    for p in procs.values():
        old = prev_procs.get(p.key)
        rate = (p.ticks - old[0]) / clk / dt if old and dt and dt >= MIN_SAMPLE else None
        p.first_seen = old[3] if old else now
        p.last_active = old[1] if old else None
        if rate is not None and rate >= active_cpu:
            p.last_active = now
        if old is None:
            age = max(now - p.start, 1e-9)
            p.busy_since = p.start if p.ticks / clk / age >= busy_cpu else None
        elif rate is None:
            p.busy_since = old[2]
        elif rate >= busy_cpu:
            p.busy_since = old[2] or prev_t
        elif rate >= busy_cpu / 2:
            p.busy_since = old[2]  # a dip under load does not end a busy period
        else:
            p.busy_since = None
        # Keep the old tick baseline when the gap was too short, so the next run measures a full window.
        base = old[0] if old and rate is None and dt is not None and dt < MIN_SAMPLE else p.ticks
        tracked[p.key] = [base, p.last_active, p.busy_since, p.first_seen]
    return tracked


# ---------- Claude sessions ----------

def claude_session_ids(procs: dict[int, Proc], proc_root: str) -> dict[int, str]:
    """Map each Claude process to its session id, read from a child's environment."""
    kids: dict[int, list[int]] = {}
    for p in procs.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    out = {}
    for p in procs.values():
        if p.agent != "claude":
            continue
        for child in sorted(kids.get(p.pid, [])):
            sid = read_env(proc_root, child).get("CLAUDE_CODE_SESSION_ID")
            if sid and SESSION_ID.fullmatch(sid):
                out[p.pid] = sid
                break
    return out


def project_dir_name(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_mtime(sid: str, cwd: str, claude_dir: str, cache: dict, now: float) -> float | None:
    """Last write time of a session transcript. Only stat() is used; contents are never read."""
    projects = os.path.join(claude_dir, "projects")
    entry = cache.get(sid)
    candidates = []
    if isinstance(entry, str):
        candidates.append(entry)
    elif isinstance(entry, (int, float)) and now - entry < TRANSCRIPT_RETRY:
        return None  # searched recently, nothing there
    if cwd:
        candidates.append(os.path.join(projects, project_dir_name(cwd), sid + ".jsonl"))
    for path in candidates:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        cache[sid] = path
        return mtime
    try:
        dirs = [e.path for e in os.scandir(projects) if e.is_dir()]
    except OSError:
        dirs = []
    for d in dirs:
        path = os.path.join(d, sid + ".jsonl")
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        cache[sid] = path
        return mtime
    cache[sid] = now
    return None


def session_name(cwd: object, sid: str) -> str:
    """A short, label-safe name for a session: its folder, without a worktree's hash suffix."""
    base = os.path.basename(str(cwd).rstrip("/")) if isinstance(cwd, str) and cwd else ""
    base = re.sub(r"-[0-9a-f]{6}$", "", base)
    base = re.sub(r"[^A-Za-z0-9._]+", "-", base).strip("-")[:48].rstrip("-")
    return base or "session-" + sid[:8]


def read_claude_status(claude_dir: str) -> dict[int, dict]:
    """Claude Code's own per-process status files (~/.claude/sessions/<pid>.json): status busy,
    waiting or idle, statusUpdatedAt in ms, the session title and the process start ticks.
    Undocumented, so callers fall back to hooks and transcript dates when a field is missing."""
    out = {}
    try:
        names = os.listdir(os.path.join(claude_dir, "sessions"))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        data = load_json(os.path.join(claude_dir, "sessions", name))
        if isinstance(data, dict) and isinstance(data.get("pid"), int):
            out[data["pid"]] = data
    return out


def read_hook_sessions(sessions_dir: str) -> list[dict]:
    out = []
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        data = load_json(os.path.join(sessions_dir, name))
        if isinstance(data, dict) and data.get("state") in STATES and isinstance(data.get("ts"), (int, float)):
            data["_file"] = os.path.join(sessions_dir, name)
            out.append(data)
    return out


# ---------- ollama ----------

def ollama_stats(url: str, timeout: float) -> dict | None:
    if not url:
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/api/ps", timeout=timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return {"up": 0}
    models = data.get("models") or []
    return {
        "up": 1,
        "models": len(models),
        "memory_bytes": sum(int(m.get("size") or 0) for m in models),
        "vram_bytes": sum(int(m.get("size_vram") or 0) for m in models),
    }


# ---------- snapshot ----------

def snapshot(args, now: float, prev: dict) -> tuple[dict, dict]:
    """Collect everything. Returns (snapshot, new persisted state)."""
    procs, clk = read_procs(args.proc, None if args.all_users else os.getuid())
    tracked = track_cpu(procs, clk, prev, now, args.active_cpu, args.runaway_cpu)
    cache = dict(prev.get("transcripts") or {})

    # Claude session ids, transcript dates and hook state, joined on the live process.
    sids = claude_session_ids(procs, args.proc)
    live_sids = {}
    activity: dict[int, float] = {}
    for pid, sid in sids.items():
        live_sids[sid] = pid
        try:
            cwd = os.readlink(os.path.join(args.proc, str(pid), "cwd"))
        except OSError:
            cwd = ""
        mtime = transcript_mtime(sid, cwd, args.claude_dir, cache, now)
        if mtime:
            activity[pid] = mtime
    cache = {k: v for k, v in cache.items() if k in live_sids}

    # Hook files, joined to the live process that wrote them (or that owns their session id).
    hooks: dict[int, dict] = {}
    stale_hook_files = []
    for h in read_hook_sessions(os.path.join(args.runtime_dir, "sessions")):
        pid = h.get("pid")
        if isinstance(pid, int) and pid in procs and procs[pid].agent and procs[pid].start_ticks == h.get("start"):
            owner = pid
        else:
            owner = live_sids.get(h.get("session_id"))
        if owner is None:
            stale_hook_files.append(h["_file"])
            continue
        if owner not in hooks or h["ts"] > hooks[owner]["ts"]:  # after /clear, keep the newest
            hooks[owner] = h

    # A process whose parent is the same agent is part of that agent (codex's node wrapper and
    # native binary, for example). One session per topmost process; the rest add memory.
    def root_of(p: Proc) -> Proc:
        while procs.get(p.ppid) is not None and procs[p.ppid].agent == p.agent:
            p = procs[p.ppid]
        return p

    kids: dict[int, list[int]] = {}
    for p in procs.values():
        kids.setdefault(p.ppid, []).append(p.pid)

    group_rss: dict[int, int] = {}
    group_active: dict[int, float] = {}
    for p in procs.values():
        if not p.agent:
            continue
        root = root_of(p)
        group_rss[root.pid] = group_rss.get(root.pid, 0) + p.rss
        if p.last_active:
            group_active[root.pid] = max(group_active.get(root.pid, 0.0), p.last_active)
        if p.pid != root.pid and p.pid in hooks and root.pid not in hooks:
            hooks[root.pid] = hooks.pop(p.pid)

    def tool_running(root: Proc, since: float) -> bool:
        """A tool started after `since` (the session's last hook event) is still running under it: a
        Claude Bash tool shell, or a busy child. Background shells started earlier do not count."""
        stack, seen = list(kids.get(root.pid, [])), 0
        while stack and seen < 2000:
            q = procs.get(stack.pop())
            seen += 1
            if q is None or q.agent == root.agent:
                continue
            if q.start >= since - 2 and (any("/shell-snapshots/" in a for a in q.argv[:3]) or q.last_active == now):
                return True
            stack.extend(kids.get(q.pid, []))
        return False

    claude_status = read_claude_status(args.claude_dir)
    CLAUDE_STATES = {"busy": "working", "waiting": "waiting", "idle": "idle"}

    agents = {name: {"processes": 0, "memory_bytes": 0, "stale": 0, "stale_memory_bytes": 0}
              for name in args.agents}
    status = {k: 0 for k in STATUSES}
    needs_you, your_turn, stale, sessions_out = [], [], [], []
    for pid, rss in group_rss.items():
        p = procs[pid]
        a = agents.setdefault(p.agent, {"processes": 0, "memory_bytes": 0, "stale": 0,
                                        "stale_memory_bytes": 0})
        a["processes"] += 1
        a["memory_bytes"] += rss
        h = hooks.get(pid)
        own = claude_status.get(pid) if p.agent == "claude" else None
        if own and str(own.get("procStart")) != str(p.start_ticks):
            own = None  # a reused pid
        if own and own.get("status") in CLAUDE_STATES and isinstance(own.get("statusUpdatedAt"), (int, float)):
            own_ts = own["statusUpdatedAt"] / 1000
            hook_says_question = (h and h["state"] == "waiting" and h.get("reason") in ("question", "plan", "permission")
                                  and h["ts"] >= own_ts - 1)
            if not hook_says_question:
                # Claude's own record is the authority; the hook only adds a reason.
                reason = h.get("reason") if h and h["state"] == "waiting" else None
                h = {"state": CLAUDE_STATES[own["status"]], "ts": own_ts, "reason": reason,
                     "name": (h or {}).get("name"), "session_id": own.get("sessionId")}
        # Transcript dates and hook events are exact. CPU is not: every Claude session wakes up at
        # once when settings.json changes, so CPU counts only when nothing exact exists.
        exact = max([t for t in (activity.get(pid), h["ts"] if h else None) if t], default=None)
        last = exact or group_active.get(pid) or p.first_seen
        idle = now - last
        if idle >= args.stale_after:
            st = "stale"
        elif h and h["state"] == "waiting":
            st = "needs_you"
        elif h and h["state"] == "working":
            st = "working" if idle < args.working_timeout or tool_running(p, h["ts"]) else "your_turn"
        elif h:
            st = "your_turn"
        else:
            # Without hooks, working needs fresh activity; first sighting and old background
            # shells are not proof.
            seen = [t for t in (exact, group_active.get(pid)) if t]
            st = "working" if any(now - t < RECENT for t in seen) else "your_turn"
        status[st] += 1
        try:
            cwd = os.readlink(os.path.join(args.proc, str(pid), "cwd"))
        except OSError:
            cwd = ""
        title = (own or {}).get("name") if own else None
        name = (session_name(title, "") if isinstance(title, str) and title.strip() else None) \
            or (h or {}).get("name") or session_name(cwd, sids.get(pid) or str(pid))
        sessions_out.append({"pid": pid, "agent": p.agent, "status": st, "name": name,
                             "idle_seconds": round(idle), "memory_bytes": rss})
        if st == "needs_you":
            needs_you.append({"name": name, "reason": h.get("reason") or "input", "agent": p.agent,
                              "seconds": round(now - h["ts"]), "session_id": h.get("session_id")})
        elif st == "your_turn":
            your_turn.append({"name": name, "agent": p.agent, "seconds": round(idle),
                              "session_id": (h or {}).get("session_id") or sids.get(pid)})
        elif st == "stale":
            a["stale"] += 1
            a["stale_memory_bytes"] += rss
            stale.append({"pid": pid, "agent": p.agent, "idle_seconds": round(idle),
                          "memory_bytes": rss, "session_id": sids.get(pid), "name": name})

    agent_pids = {p.pid for p in procs.values() if p.agent}

    def agent_related(p: Proc) -> bool:
        q, hops = p.ppid, 0
        while q > 1 and hops < 64:
            if q in agent_pids:
                return True
            parent = procs.get(q)
            if parent is None:
                break
            q, hops = parent.ppid, hops + 1
        env = read_env(args.proc, p.pid)
        return any(k in env for k in AGENT_ENV_MARKERS)

    runaway = []
    for p in procs.values():
        if p.agent or p.pid == os.getpid() or not p.busy_since:
            continue
        busy = now - p.busy_since
        if busy >= args.runaway_after and agent_related(p):
            runaway.append({"pid": p.pid, "busy_seconds": round(busy), "cmd": short_cmd(p.argv)})
    runaway.sort(key=lambda r: -r["busy_seconds"])

    snap = {
        "time": round(now, 3),
        "agents": agents,
        "status": status,
        "needs_you": sorted(needs_you, key=lambda w: -w["seconds"]),
        "your_turn": sorted(your_turn, key=lambda w: -w["seconds"]),
        "sessions": sorted(sessions_out, key=lambda x: (STATUSES.index(x["status"]), x["idle_seconds"])),
        "stale": sorted(stale, key=lambda s: -s["idle_seconds"]),
        "runaway": runaway,
        "ollama": ollama_stats(args.ollama_url, args.timeout),
        "_stale_hook_files": stale_hook_files,
    }
    state = {"t": now, "procs": tracked, "transcripts": cache}
    return snap, state


# ---------- output ----------

def render_prom(snap: dict) -> str:
    lines: list[str] = []

    def family(name: str, help_text: str, samples: list[tuple[dict, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            lab = ",".join(f'{k}="{v}"' for k, v in labels.items())
            v = int(value) if float(value).is_integer() else round(value, 3)
            lines.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")

    agents = snap["agents"]
    family("agent_processes", "Running coding-agent processes, by agent.",
           [({"agent": n}, a["processes"]) for n, a in agents.items()])
    family("agent_memory_bytes", "Resident memory of agent processes, by agent.",
           [({"agent": n}, a["memory_bytes"]) for n, a in agents.items()])
    family("agent_stale_memory_bytes", "Resident memory held by stale agent sessions.",
           [({}, sum(a["stale_memory_bytes"] for a in agents.values()))])
    family("agent_sessions", "Agent sessions by status: working, needs you, your turn, stale. "
           "Each session is in exactly one.",
           [({"status": st}, snap["status"][st]) for st in STATUSES])
    family("agent_needs_you_longest_seconds", "How long the longest-waiting session has needed you.",
           [({}, snap["needs_you"][0]["seconds"] if snap["needs_you"] else 0)])
    shown, used = [], set()
    for w in snap["needs_you"][:MAX_WAITING_SERIES]:
        name, n = w["name"], 2
        while name in used:
            name, n = f"{w['name']}-{n}", n + 1
        used.add(name)
        shown.append(({"reason": w["reason"], "session": name}, w["seconds"]))
    if shown:
        family("agent_needs_you_session_seconds",
               "Seconds each session has needed you, by reason and folder name. At most 5.", shown)
    shown, used = [], set()
    for w in snap["your_turn"][:MAX_WAITING_SERIES]:
        name, n = w["name"], 2
        while name in used:
            name, n = f"{w['name']}-{n}", n + 1
        used.add(name)
        shown.append(({"session": name}, w["seconds"]))
    if shown:
        family("agent_your_turn_session_seconds",
               "Seconds since each your-turn session last did anything, oldest first, by name. At most 5.", shown)
    family("agent_runaway_processes",
           "Processes started by agents that kept a CPU core busy past the runaway period.",
           [({}, len(snap["runaway"]))])
    family("agent_runaway_longest_seconds", "How long the longest runaway process has been busy.",
           [({}, snap["runaway"][0]["busy_seconds"] if snap["runaway"] else 0)])
    o = snap["ollama"]
    if o is not None:
        family("ollama_up", "1 if the ollama API answered.", [({}, o["up"])])
        if o["up"]:
            family("ollama_models_loaded", "Models ollama holds in memory.", [({}, o["models"])])
            family("ollama_memory_bytes", "Memory used by loaded ollama models.",
                   [({}, o["memory_bytes"])])
            family("ollama_vram_bytes", "GPU memory used by loaded ollama models.",
                   [({}, o["vram_bytes"])])
    return "\n".join(lines) + "\n"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def render_text(snap: dict) -> str:
    out = []
    for name, a in snap["agents"].items():
        out.append(f"{name:<8} {a['processes']:>4} procs  {human_bytes(a['memory_bytes']):>9}"
                   f"  {a['stale']} stale ({human_bytes(a['stale_memory_bytes'])})")
    st = snap["status"]
    out.append(f"status   working {st['working']}  needs you {st['needs_you']}  "
               f"your turn {st['your_turn']}  stale {st['stale']}")
    for w in snap["needs_you"]:
        out.append(f"needs    {w['name']}  {w['reason']}  {human_secs(w['seconds'])}")
    if snap["runaway"]:
        for r in snap["runaway"]:
            out.append(f"runaway  pid {r['pid']} busy {human_secs(r['busy_seconds'])}  {r['cmd']}")
    else:
        out.append("runaway  none")
    o = snap["ollama"]
    if o is not None:
        out.append("ollama   down" if not o["up"] else
                   f"ollama   up, {o['models']} models, {human_bytes(o['memory_bytes'])}")
    return "\n".join(out) + "\n"


def public(snap: dict) -> dict:
    return {k: v for k, v in snap.items() if not k.startswith("_")}


# ---------- commands ----------

def run_collect(args) -> int:
    state_path = os.path.join(args.runtime_dir, "state.json")
    prev = load_json(state_path) or {}
    now = time.time()
    snap, state = snapshot(args, now, prev)
    text = render_prom(snap)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        write_atomic(args.out, text)
    if not prev.get("t") or now - prev["t"] >= MIN_SAMPLE:
        write_atomic(state_path, json.dumps(state, separators=(",", ":")))
    for path in snap["_stale_hook_files"]:
        try:
            if now - os.stat(path).st_mtime > 3600:
                os.unlink(path)
        except OSError:
            pass
    if args.json:
        print(json.dumps(public(snap)))
    return 0


def cmd_collect(args) -> int:
    try:
        return run_collect(args)
    except OSError as e:
        print(f"agent-exporter: cannot write: {e}. Try: --out PATH in a writable directory", file=sys.stderr)
        return 1


def cmd_run(args) -> int:
    while True:
        started = time.monotonic()
        try:
            run_collect(args)
        except Exception as e:  # keep the daemon alive; the next cycle may succeed
            print(f"agent-exporter: collect failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(max(1.0, args.interval - (time.monotonic() - started)))


def cmd_show(args) -> int:
    prev = load_json(os.path.join(args.runtime_dir, "state.json")) or {}
    snap, _ = snapshot(args, time.time(), prev)
    if not prev:
        print("No earlier run found, so CPU-based activity is not known yet. "
              "Try: agent-exporter collect, then show again.", file=sys.stderr)
    sys.stdout.write(json.dumps(public(snap)) + "\n" if args.json else render_text(snap))
    return 0


def hook_state(event: str, payload: dict) -> tuple[str, str | None] | None:
    """Map a hook event to (state, reason). None leaves the state as it was.

    Claude Code and Codex share event names. Gemini CLI uses BeforeAgent, AfterAgent, BeforeTool
    and AfterTool. OpenCode's plugin passes its bus events as they are (session.status and so on).
    """
    if event in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "BeforeAgent", "AfterTool",
                 "permission.replied", "question.replied", "question.rejected"):
        return "working", None
    if event in ("PreToolUse", "BeforeTool"):
        reason = WAITING_TOOLS.get(payload.get("tool_name"))
        return ("waiting", reason) if reason else None
    if event in ("PermissionRequest", "permission.asked"):
        return "waiting", "permission"
    if event == "question.asked":
        return "waiting", "question"
    if event == "session.status":
        kind = payload.get("status")
        return {"busy": ("working", None), "retry": ("working", None), "idle": ("idle", None)}.get(kind)
    if event in ("SessionStart", "Stop", "StopFailure", "AfterAgent", "session.idle", "session.error"):
        return "idle", None
    if event == "Notification":
        kind = payload.get("notification_type")
        if kind in NOTIFICATION_REASONS:
            return "waiting", NOTIFICATION_REASONS[kind]
        return ("idle", None) if kind == "idle_prompt" else None
    if event in ("SessionEnd", "session.deleted"):
        return "ended", None
    return None


def read_agent(proc: str, pid: int) -> tuple[str | None, int, int] | None:
    """(agent name or None, parent pid, start ticks) for a live process, or None."""
    d = os.path.join(proc, str(pid))
    try:
        with open(os.path.join(d, "stat")) as f:
            stat = f.read()
        with open(os.path.join(d, "cmdline"), "rb") as f:
            argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        try:
            exe = os.readlink(os.path.join(d, "exe"))
        except OSError:
            exe = ""
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    return agent_name(argv, exe), int(fields[1]), int(fields[19])


def find_agent_ancestor(proc: str, start_pid: int) -> tuple[int, int] | None:
    """The nearest ancestor that is a coding agent: (pid, start ticks)."""
    pid, hops = start_pid, 0
    while pid > 1 and hops < 16:
        found = read_agent(proc, pid)
        if found is None:
            return None
        name, ppid, start = found
        if name:
            return pid, start
        pid, hops = ppid, hops + 1
    return None


def hook_owner(payload: dict, proc: str, parent_pid: int) -> tuple[int, int] | None:
    """The agent process a hook event belongs to: (pid, start ticks).

    A plugin that runs inside the agent (OpenCode's) names its own pid, because its helper can
    outlive the agent and be adopted by an unrelated process, such as a Claude session, so walking
    up from it would credit the wrong agent. Hooks the agent waits on walk up to it."""
    pid = payload.get("agent_pid")
    if pid is None:
        return find_agent_ancestor(proc, parent_pid)
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    found = read_agent(proc, pid)
    if found is None or found[0] is None or found[0] != payload.get("agent", found[0]):
        return None
    return pid, found[2]


def record_hook(payload: dict, sessions_dir: str, proc: str, now: float, parent_pid: int) -> None:
    # A plugin's own timestamp orders its events better than when each helper got to run; trust it
    # only when it is close to now.
    sent = payload.get("ts")
    if isinstance(sent, (int, float)) and not isinstance(sent, bool) and abs(sent - now) < 60:
        now = float(sent)
    sid = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) or not isinstance(event, str):
        return
    mapped = hook_state(event, payload)
    if mapped is None:
        return
    state, reason = mapped
    os.makedirs(sessions_dir, exist_ok=True)
    path = os.path.join(sessions_dir, sid + ".json")
    with open(os.path.join(sessions_dir, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = load_json(path)
        if isinstance(old, dict) and isinstance(old.get("ts"), (int, float)) and old["ts"] > now:
            return  # a later event already wrote; async hooks can finish out of order
        if state == "ended":
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        owner = hook_owner(payload, proc, parent_pid)
        record = {"session_id": sid, "state": state, "ts": now, "event": event, "reason": reason,
                  "notification_type": payload.get("notification_type"),
                  "name": session_name(payload.get("cwd"), sid) if payload.get("cwd")
                  else (old or {}).get("name") or session_name(None, sid)}
        if owner:
            record["pid"], record["start"] = owner
        elif isinstance(old, dict) and "pid" in old:
            record["pid"], record["start"] = old.get("pid"), old.get("start")
        write_atomic(path, json.dumps(record))


def cmd_hook(args) -> int:
    """Called by agent hooks. Prints nothing and always exits 0, so it can never block an agent."""
    now = time.time()
    try:
        payload = json.loads(sys.stdin.read() or "{}")  # PostToolUse can carry megabytes
        if isinstance(payload, dict):
            record_hook(payload, os.path.join(args.runtime_dir, "sessions"), args.proc, now, os.getppid())
    except Exception:
        pass
    return 0


def hook_command() -> str:
    found = shutil.which("agent-exporter")
    if found:
        return f"{shlex.quote(found)} hook"
    return f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} hook"


def is_ours(handler: dict) -> bool:
    return isinstance(handler, dict) and bool(HOOK_MARK.search(str(handler.get("command", ""))))


def edit_settings(path: str, install: bool, command: str, dry_run: bool,
                  events: dict = HOOK_EVENTS, handler: dict | None = None) -> dict:
    """Add or remove our hooks in a Claude-style settings file: Claude Code's settings.json,
    Codex's hooks.json or Gemini CLI's settings.json. `handler` holds the extra handler fields."""
    handler = {"async": True, "timeout": 10} if handler is None else handler
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
        except ValueError as e:
            raise SystemExit(f"agent-exporter: {path} is not valid JSON ({e}); not touching it")
        if not isinstance(data, dict):
            raise SystemExit(f"agent-exporter: {path} is not a JSON object; not touching it")
    else:
        data = {}
    hooks = data.setdefault("hooks", {}) if install else data.get("hooks") or {}
    changed, unchanged = [], []
    for event, matcher in events.items():
        groups = hooks.get(event) or []
        ours = any(is_ours(h) for g in groups if isinstance(g, dict) for h in g.get("hooks") or [])
        if install:
            if ours:
                unchanged.append(event)
                continue
            group = {"hooks": [{"type": "command", "command": command, **handler}]}
            if matcher:
                group = {"matcher": matcher, **group}
            groups.append(group)
            hooks[event] = groups
            changed.append(event)
        else:
            if not ours:
                continue
            kept = []
            for g in groups:
                if not isinstance(g, dict):
                    kept.append(g)
                    continue
                rest = [h for h in g.get("hooks") or [] if not is_ours(h)]
                if rest:
                    kept.append(dict(g, hooks=rest))
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
            changed.append(event)
    if not install and "hooks" in data and not data["hooks"]:
        data.pop("hooks")
    backup = None
    path = os.path.realpath(path)  # keep a symlinked settings file a symlink
    if changed and not dry_run:
        if os.path.exists(path):
            backup = f"{path}.bak-agent-exporter-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(path, backup)
        write_atomic(path, json.dumps(data, indent=2) + "\n")
    return {"settings": path, "changed": changed, "unchanged": unchanged, "dry_run": dry_run,
            "backup": backup, "command": command if install else None}


OPENCODE_PLUGIN = """// agent-exporter: tells agent-exporter when an OpenCode session is working, needs you or is idle.
// Written by `agent-exporter install-hooks --agent opencode`; removed by uninstall-hooks.
import { spawn } from "node:child_process"

const COMMAND = __COMMAND__
// Each event carries the time OpenCode saw it, because the helpers it starts can finish out of order.
// Events that change a session's status. Child sessions (subagents) are skipped, so a finished
// subagent never makes the main session look idle.
const EVENTS = new Set(["session.status", "session.idle", "session.error", "session.deleted",
  "permission.asked", "permission.replied", "question.asked", "question.replied", "question.rejected"])

export const AgentExporter = async ({ directory }) => {
  const children = new Set()
  const send = (payload) => {
    try {
      const child = spawn(COMMAND, { shell: true, stdio: ["pipe", "ignore", "ignore"] })
      child.on("error", () => {})
      child.stdin.on("error", () => {})
      child.stdin.end(JSON.stringify(payload))
    } catch {}
  }
  return {
    event: async ({ event }) => {
      const p = event.properties || {}
      if (event.type === "session.created" || event.type === "session.updated") {
        if (p.info && p.info.parentID) children.add(p.info.id)
        return
      }
      if (!EVENTS.has(event.type)) return
      const sid = p.sessionID || (p.info && p.info.id)
      if (!sid || children.has(sid)) return
      send({ session_id: sid, hook_event_name: event.type, cwd: directory,
             status: p.status && p.status.type, agent: "opencode", agent_pid: process.pid,
             ts: (performance.timeOrigin + performance.now()) / 1000 })
    },
  }
}
"""


def edit_plugin(path: str, install: bool, command: str, dry_run: bool) -> dict:
    """Write or remove the OpenCode plugin. A file without our first line is never touched."""
    text = OPENCODE_PLUGIN.replace("__COMMAND__", json.dumps(command))
    mark = OPENCODE_PLUGIN.splitlines()[0]
    try:
        with open(path) as f:
            current = f.read()
    except OSError:
        current = None
    if current is not None and not current.startswith(mark):
        raise SystemExit(f"agent-exporter: {path} exists and is not ours; not touching it")
    changed = (current != text) if install else current is not None
    if changed and not dry_run:
        if install:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_atomic(path, text)
        else:
            os.unlink(path)
    return {"settings": path, "changed": ["plugin"] if changed else [],
            "unchanged": [] if changed or not install else ["plugin"], "dry_run": dry_run,
            "backup": None, "command": command if install else None}


def cmd_hooks(args) -> int:
    install = args.command == "install-hooks"
    path = args.settings or hook_file(args.agent)
    command = args.hook_command or hook_command()
    if args.agent == "opencode":
        receipt = edit_plugin(path, install, command, args.dry_run)
    else:
        events, handler = {"claude": (HOOK_EVENTS, None), "codex": (CODEX_HOOK_EVENTS, {"timeout": 10}),
                           "gemini": (GEMINI_HOOK_EVENTS, {"timeout": 10000})}[args.agent]
        receipt = edit_settings(path, install, command, args.dry_run, events, handler)
    receipt["agent"] = args.agent
    # Codex runs a new or changed hook only after the person trusts it once in its /hooks screen.
    if args.agent == "codex" and install and receipt["changed"]:
        receipt["next"] = "open codex, run /hooks and trust the agent-exporter hooks"
    args.settings = path
    if args.json:
        print(json.dumps(receipt))
    else:
        verb = ("would " if args.dry_run else "") + ("add" if install else "remove")
        if receipt["changed"]:
            print(f"{verb} hooks for {', '.join(receipt['changed'])} in {args.settings}"
                  + (f" (backup {receipt['backup']})" if receipt["backup"] else ""))
        else:
            print(f"nothing to change in {args.settings}")
        if receipt.get("next"):
            print(f"next: {receipt['next']}")
    return 0


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--runtime-dir", default=runtime_home(),
                        help="where hook state and CPU samples live (default: %(default)s)")
    common.add_argument("--proc", default="/proc", help=argparse.SUPPRESS)

    collect = argparse.ArgumentParser(add_help=False, parents=[common])
    collect.add_argument("--out", default=os.path.join(state_home(), "agents.prom"),
                         help="Prometheus text file to write, or - for stdout (default: %(default)s)")
    collect.add_argument("--agents", default=",".join(DEFAULT_AGENTS),
                         type=parse_agents,
                         help="agents always reported, even at zero (default: %(default)s)")
    collect.add_argument("--stale-after", type=parse_duration, default=parse_duration("24h"),
                         help="idle time after which an agent process counts as stale (default: 24h)")
    collect.add_argument("--working-timeout", type=parse_duration, default=float(WORKING_TIMEOUT),
                         help="a working session silent this long counts as your turn (default: 30m)")
    collect.add_argument("--runaway-after", type=parse_duration, default=parse_duration("1h"),
                         help="busy time after which an agent's child counts as runaway (default: 1h)")
    collect.add_argument("--runaway-cpu", type=float, default=0.9,
                         help="CPU cores that count as busy (default: %(default)s)")
    collect.add_argument("--active-cpu", type=float, default=0.05,
                         help="CPU cores between runs that count as activity (default: %(default)s)")
    collect.add_argument("--ollama-url", default="http://127.0.0.1:11434",
                         help="ollama API base URL, empty to skip (default: %(default)s)")
    collect.add_argument("--timeout", type=float, default=1.0,
                         help="seconds to wait for ollama (default: %(default)s)")
    collect.add_argument("--claude-dir", default=claude_home(), help=argparse.SUPPRESS)
    collect.add_argument("--all-users", action="store_true",
                         help="scan every user's processes (needs root)")
    collect.add_argument("--json", action="store_true", help="print the snapshot as JSON")

    p = argparse.ArgumentParser(prog="agent-exporter", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"agent-exporter {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", parents=[collect], help="write the .prom file once")
    r = sub.add_parser("run", parents=[collect], help="write the .prom file every --interval")
    r.add_argument("--interval", type=parse_duration, default=parse_duration("20s"),
                   help="time between writes (default: 20s)")
    sub.add_parser("show", parents=[collect], help="print the current snapshot, write nothing")
    sub.add_parser("hook", parents=[common], help="record an agent's hook event from stdin")
    for name, text in (("install-hooks", "add the hooks to an agent's settings"),
                       ("uninstall-hooks", "remove the hooks from an agent's settings")):
        h = sub.add_parser(name, help=text)
        h.add_argument("--agent", choices=HOOK_AGENTS, default="claude",
                       help="which agent (default: %(default)s)")
        h.add_argument("--settings",
                       help="file to edit (default: the agent's own, such as ~/.codex/hooks.json)")
        h.add_argument("--hook-command", help="command the hooks run (default: this program + hook)")
        h.add_argument("--dry-run", action="store_true", help="report the change, write nothing")
        h.add_argument("--json", action="store_true", help="print the receipt as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "show":
        return cmd_show(args)
    if args.command == "hook":
        return cmd_hook(args)
    return cmd_hooks(args)


if __name__ == "__main__":
    sys.exit(main())
