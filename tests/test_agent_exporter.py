"""Tests run against a fake /proc tree, a fake ~/.claude and a fake ollama server."""
import http.server
import io
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_exporter as ae  # noqa: E402

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
BTIME = 1_000_000
NOW = BTIME + 10 * 86400  # ten days after boot


class FakeProc:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "stat"), "w") as f:
            f.write(f"cpu 1 2 3\nbtime {BTIME}\n")

    def add(self, pid, argv, *, ppid=1, exe="", age=3600.0, cpu=0.0, rss_mb=100, env=None,
            cwd=None, comm=None):
        """age: seconds since start at NOW; cpu: average cores over that age."""
        d = os.path.join(self.root, str(pid))
        os.makedirs(d, exist_ok=True)
        start_ticks = int((NOW - age - BTIME) * CLK)
        total = int(cpu * age * CLK)
        self.write_stat(pid, ppid, total, start_ticks, rss_mb, comm or os.path.basename(argv[0]))
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()))
        if exe:
            os.symlink(exe, os.path.join(d, "exe"))
        if cwd:
            os.symlink(cwd, os.path.join(d, "cwd"))
        return start_ticks

    def write_stat(self, pid, ppid, ticks, start_ticks, rss_mb, comm="x"):
        rss_pages = rss_mb * 1024 * 1024 // PAGE
        fields = ["S", ppid, 0, 0, 0, -1, 0, 0, 0, 0, 0, ticks, 0, 0, 0, 20, 0, 1, 0,
                  start_ticks, 0, rss_pages]
        with open(os.path.join(self.root, str(pid), "stat"), "w") as f:
            f.write(f"{pid} ({comm}) " + " ".join(str(x) for x in fields) + "\n")

    def set_ticks(self, pid, ticks):
        path = os.path.join(self.root, str(pid), "stat")
        text = open(path).read()
        head, rest = text[: text.rfind(")") + 2], text[text.rfind(")") + 2:].split()
        rest[11] = str(ticks)
        open(path, "w").write(head + " ".join(rest) + "\n")


def make_args(tmp_path, **kw):
    args = SimpleNamespace(
        proc=str(tmp_path / "proc"), runtime_dir=str(tmp_path / "rt"),
        claude_dir=str(tmp_path / "claude"), all_users=False, agents=("claude", "codex"),
        stale_after=86400.0, runaway_after=3600.0, runaway_cpu=0.9, active_cpu=0.05,
        ollama_url="", timeout=1.0, out=str(tmp_path / "out" / "agents.prom"), json=False)
    for k, v in kw.items():
        setattr(args, k, v)
    return args


CCD = "/home/u/.claude/remote/ccd-cli/2.1.308-abc"


# ---------- agent detection ----------

@pytest.mark.parametrize("argv,exe,want", [
    ([CCD, "--output-format", "stream-json"], CCD, "claude"),
    (["claude"], "/home/u/.local/share/claude/versions/2.1.300", "claude"),
    (["/usr/bin/claude", "-p", "hi"], "", "claude"),
    (["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"], "/usr/bin/node", "claude"),
    (["node", "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"], "/usr/bin/node", "codex"),
    (["/usr/local/bin/codex", "exec"], "", "codex"),
    (["node", "--no-warnings", "/opt/gemini.js"], "", "gemini"),
    (["python3", "-I", "/x/aider.py"], "", "aider"),
    (["codex-code-mode"], "", None),
    (["bash", "-c", "claude"], "/usr/bin/bash", None),
    (["node", "/srv/app/server.js"], "", None),
    ([], "", None),
])
def test_agent_name(argv, exe, want):
    assert ae.agent_name(argv, exe) == want


# ---------- snapshot ----------

def test_counts_memory_and_wrapper_pairs(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(10, [CCD], exe=CCD, rss_mb=200)
    fp.add(11, [CCD], exe=CCD, rss_mb=300)
    fp.add(20, ["node", "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"], rss_mb=50)
    fp.add(21, ["/usr/local/lib/node_modules/@openai/codex/vendor/codex"], ppid=20, rss_mb=70)
    fp.add(30, ["bash"], rss_mb=5)
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert snap["agents"]["claude"]["processes"] == 2
    assert snap["agents"]["claude"]["memory_bytes"] == 500 * 1024 * 1024
    assert snap["agents"]["codex"]["processes"] == 1, "wrapper and native binary are one agent"
    assert snap["agents"]["codex"]["memory_bytes"] == 120 * 1024 * 1024


def test_stale_from_transcript_date(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    cwd = "/home/u/src/proj.x"
    for pid, sid in ((10, "aaaa-1111"), (11, "bbbb-2222")):
        fp.add(pid, [CCD], exe=CCD, age=9 * 86400, rss_mb=100, cwd=cwd)
        fp.add(pid + 100, ["python3", "mcp.py"], ppid=pid, env={"CLAUDE_CODE_SESSION_ID": sid})
    proj = tmp_path / "claude" / "projects" / "-home-u-src-proj-x"
    proj.mkdir(parents=True)
    old, new = proj / "aaaa-1111.jsonl", proj / "bbbb-2222.jsonl"
    old.write_text("secret transcript text")
    new.write_text("secret transcript text")
    os.utime(old, (NOW - 3 * 86400, NOW - 3 * 86400))
    os.utime(new, (NOW - 600, NOW - 600))
    snap, state = ae.snapshot(make_args(tmp_path), NOW, {})
    assert snap["agents"]["claude"]["stale"] == 1
    assert [s["session_id"] for s in snap["stale"]] == ["aaaa-1111"]
    assert snap["stale"][0]["idle_seconds"] == 3 * 86400
    assert state["transcripts"]["aaaa-1111"] == str(old)


def test_transcript_found_outside_cwd_project(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(10, [CCD], exe=CCD, age=9 * 86400, cwd="/somewhere/else")
    fp.add(110, ["uv"], ppid=10, env={"CLAUDE_CODE_SESSION_ID": "cccc-3333"})
    other = tmp_path / "claude" / "projects" / "-moved"
    other.mkdir(parents=True)
    t = other / "cccc-3333.jsonl"
    t.write_text("x")
    os.utime(t, (NOW - 2 * 86400, NOW - 2 * 86400))
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert snap["agents"]["claude"]["stale"] == 1


def test_no_evidence_is_not_stale_until_observed_idle(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(20, ["/usr/local/bin/codex"], age=30 * 86400, cpu=0.0)
    args = make_args(tmp_path)
    snap, state = ae.snapshot(args, NOW, {})
    assert snap["agents"]["codex"]["stale"] == 0, "first sighting gives no idle evidence"
    snap, _ = ae.snapshot(args, NOW + 86400 + 1, state)
    assert snap["agents"]["codex"]["stale"] == 1


def test_cpu_activity_resets_staleness(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(20, ["/usr/local/bin/codex"], age=30 * 86400, cpu=0.0)
    args = make_args(tmp_path)
    _, state = ae.snapshot(args, NOW, {})
    fp.set_ticks(20, 20 * CLK)  # 20 CPU seconds over the next 20 seconds
    snap, state = ae.snapshot(args, NOW + 20, state)
    snap, _ = ae.snapshot(args, NOW + 20 + 86400 - 5, state)
    assert snap["agents"]["codex"]["stale"] == 0


# ---------- runaway ----------

def test_runaway_orphan_with_agent_env(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(50, ["python", "-I", "read.py"], age=3 * 86400, cpu=1.0, env={"PI_CODING_AGENT": "1"})
    fp.add(51, ["python", "busy.py"], age=3 * 86400, cpu=1.0)  # busy but not an agent's
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert [r["pid"] for r in snap["runaway"]] == [50]
    assert snap["runaway"][0]["busy_seconds"] == 3 * 86400


def test_runaway_descendant_of_agent(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(10, [CCD], exe=CCD)
    fp.add(11, ["bash"], ppid=10)
    fp.add(12, ["node", "build.js"], ppid=11, age=2 * 3600, cpu=0.95)
    fp.add(13, ["node", "short.js"], ppid=11, age=600, cpu=1.0)  # busy, but only 10 minutes
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert [r["pid"] for r in snap["runaway"]] == [12]


def test_busy_agent_itself_is_not_runaway(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(10, [CCD], exe=CCD, age=5 * 3600, cpu=1.0, env={"CLAUDECODE": "1"})
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert snap["runaway"] == []


def test_runaway_clears_when_it_goes_quiet(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(50, ["python", "x.py"], age=2 * 86400, cpu=1.0, env={"CLAUDECODE": "1"})
    args = make_args(tmp_path)
    snap, state = ae.snapshot(args, NOW, {})
    assert len(snap["runaway"]) == 1
    snap, _ = ae.snapshot(args, NOW + 60, state)  # ticks did not move: idle for a minute
    assert snap["runaway"] == []


# ---------- hooks ----------

def hook(tmp_path, payload, now, parent_pid=999):
    ae.record_hook(payload, str(tmp_path / "rt" / "sessions"), str(tmp_path / "proc"), now, parent_pid)


def test_hook_states_and_waiting_time(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    start = fp.add(10, [CCD], exe=CCD)
    fp.add(999, ["sh", "-c", "agent-exporter hook"], ppid=10)
    hook(tmp_path, {"session_id": "s1", "hook_event_name": "UserPromptSubmit"}, NOW - 100)
    hook(tmp_path, {"session_id": "s1", "hook_event_name": "Notification",
                    "notification_type": "permission_prompt"}, NOW - 90)
    rec = json.load(open(tmp_path / "rt" / "sessions" / "s1.json"))
    assert (rec["state"], rec["pid"], rec["start"]) == ("waiting", 10, start)
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert snap["sessions"]["waiting"] == 1
    assert snap["sessions"]["waiting_longest_seconds"] == 90
    hook(tmp_path, {"session_id": "s1", "hook_event_name": "PostToolUse"}, NOW - 80)
    hook(tmp_path, {"session_id": "s1", "hook_event_name": "Stop"}, NOW - 70)
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert (snap["sessions"]["idle"], snap["sessions"]["waiting"]) == (1, 0)
    hook(tmp_path, {"session_id": "s1", "hook_event_name": "SessionEnd"}, NOW - 60)
    assert not (tmp_path / "rt" / "sessions" / "s1.json").exists()


def test_hook_out_of_order_write_is_ignored(tmp_path):
    FakeProc(tmp_path / "proc")
    hook(tmp_path, {"session_id": "s2", "hook_event_name": "Stop"}, NOW)
    hook(tmp_path, {"session_id": "s2", "hook_event_name": "PostToolUse"}, NOW - 1)
    assert json.load(open(tmp_path / "rt" / "sessions" / "s2.json"))["state"] == "idle"


def test_dead_session_not_counted(tmp_path):
    FakeProc(tmp_path / "proc")  # no claude process alive
    hook(tmp_path, {"session_id": "gone", "hook_event_name": "UserPromptSubmit"}, NOW - 10)
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    assert snap["sessions"]["working"] == 0
    assert snap["_stale_hook_files"]


@pytest.mark.parametrize("payload", [
    {"session_id": "../../etc/passwd", "hook_event_name": "Stop"},
    {"session_id": ".hidden", "hook_event_name": "Stop"},
    {"session_id": "ok", "hook_event_name": "PreCompact"},
    {"hook_event_name": "Stop"},
])
def test_hook_ignores_bad_or_irrelevant_input(tmp_path, payload):
    FakeProc(tmp_path / "proc")
    hook(tmp_path, payload, NOW)
    d = tmp_path / "rt" / "sessions"
    assert not d.exists() or not [n for n in os.listdir(d) if n.endswith(".json")]


@pytest.mark.parametrize("stdin", ["not json", "[1,2]", "", '{"session_id": 5}'])
def test_hook_command_is_silent_and_exits_zero(tmp_path, monkeypatch, capsys, stdin):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert ae.main(["hook", "--runtime-dir", str(tmp_path / "rt"), "--proc", str(tmp_path)]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# ---------- output ----------

def parse_prom(text):
    """Minimal reader that enforces what Beszel #2562 needs from us."""
    families, series = {}, []
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split()
            families[name] = kind
        elif line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            float(value)
            name = key.split("{")[0]
            assert name in families, f"sample before its TYPE: {line}"
            series.append(key)
    return families, series


def test_prom_output_shape(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(10, [CCD], exe=CCD)
    snap, _ = ae.snapshot(make_args(tmp_path), NOW, {})
    snap["ollama"] = {"up": 1, "models": 2, "memory_bytes": 5, "vram_bytes": 0}
    families, series = parse_prom(ae.render_prom(snap))
    assert set(families.values()) == {"gauge"}
    assert len(series) == len(set(series)), "duplicate series"
    assert len(series) <= 64, "Beszel #2562 keeps 64 series per agent by default"
    assert 'agent_processes{agent="claude"}' in series
    assert "ollama_models_loaded" in series


def test_collect_writes_atomically_and_never_leaves_temp(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(10, [CCD], exe=CCD)
    args = make_args(tmp_path)
    assert ae.cmd_collect(args) == 0
    out_dir = tmp_path / "out"
    assert sorted(os.listdir(out_dir)) == ["agents.prom"]
    assert "agent_processes" in (out_dir / "agents.prom").read_text()
    assert (tmp_path / "rt" / "state.json").exists()


def test_temp_file_name_never_ends_in_prom(tmp_path, monkeypatch):
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (seen.append(a), real(a, b)))
    ae.write_atomic(str(tmp_path / "agents.prom"), "x 1\n")
    assert seen and not seen[0].endswith(".prom"), "Beszel would read a half-written file"


def test_collect_unwritable_out_exits_1(tmp_path, capsys):
    fp = FakeProc(tmp_path / "proc")
    fp.add(10, [CCD], exe=CCD)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    args = make_args(tmp_path, out=str(blocker / "agents.prom"))
    assert ae.cmd_collect(args) == 1
    assert "Try: --out" in capsys.readouterr().err


def test_bad_duration_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        ae.main(["collect", "--stale-after", "soon"])
    assert e.value.code == 2


# ---------- ollama ----------

def test_ollama_stats_from_api(tmp_path):
    body = json.dumps({"models": [{"name": "a", "size": 100, "size_vram": 40},
                                  {"name": "b", "size": 50, "size_vram": 0}]}).encode()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/ps"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = ae.ollama_stats(f"http://127.0.0.1:{srv.server_port}", 2)
    finally:
        srv.shutdown()
    assert got == {"up": 1, "models": 2, "memory_bytes": 150, "vram_bytes": 40}


def test_ollama_down_and_disabled():
    assert ae.ollama_stats("http://127.0.0.1:9", 0.5) == {"up": 0}
    assert ae.ollama_stats("", 0.5) is None


# ---------- settings ----------

def test_install_hooks_idempotent_and_keeps_existing(tmp_path):
    path = tmp_path / "settings.json"
    existing = {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
    path.write_text(json.dumps(existing))
    cmd = "/home/u/.local/bin/agent-exporter hook"
    r1 = ae.edit_settings(str(path), True, cmd, False)
    assert r1["changed"] == list(ae.HOOK_EVENTS) and r1["backup"]
    r2 = ae.edit_settings(str(path), True, cmd, False)
    assert r2["changed"] == [] and r2["unchanged"] == list(ae.HOOK_EVENTS)
    data = json.loads(path.read_text())
    assert data["model"] == "x"
    stop = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert stop == ["say done", cmd]
    ours = data["hooks"]["PostToolUse"][0]["hooks"][0]
    assert ours["async"] is True
    r3 = ae.edit_settings(str(path), False, cmd, False)
    assert set(r3["changed"]) == set(ae.HOOK_EVENTS)
    data = json.loads(path.read_text())
    assert data["hooks"] == existing["hooks"]


def test_install_hooks_dry_run_and_bad_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    r = ae.edit_settings(str(path), True, "agent-exporter hook", True)
    assert r["changed"] and json.loads(path.read_text()) == {}
    path.write_text("{oops")
    with pytest.raises(SystemExit):
        ae.edit_settings(str(path), True, "agent-exporter hook", False)
    assert path.read_text() == "{oops"


def test_hook_mark_matches_both_launch_forms():
    assert ae.is_ours({"command": "/home/u/.local/bin/agent-exporter hook"})
    assert ae.is_ours({"command": "/usr/bin/python3 /src/agent_exporter.py hook"})
    assert not ae.is_ours({"command": "my-agent-exporter-wrapper hookup"})


def test_short_gap_between_runs_changes_nothing(tmp_path):
    """A manual run a second after the service must not read tick rounding as activity or idleness."""
    fp = FakeProc(tmp_path / "proc")
    fp.add(20, ["/usr/local/bin/codex"], age=30 * 86400, cpu=0.0)
    fp.add(50, ["python", "x.py"], age=2 * 86400, cpu=1.0, env={"CLAUDECODE": "1"})
    args = make_args(tmp_path)
    _, state = ae.snapshot(args, NOW, {})
    busy_ticks = int(1.0 * 2 * 86400 * CLK)
    fp.set_ticks(20, 1)            # one stray tick in half a second looks like 0.02 to 0.2 cores
    fp.set_ticks(50, busy_ticks)   # no ticks counted yet in half a second looks idle
    snap, state2 = ae.snapshot(args, NOW + 0.5, state)
    assert [r["pid"] for r in snap["runaway"]] == [50]
    assert state2["procs"]["20:" + str(int((NOW - 30 * 86400 - BTIME) * CLK))][1] is None
    snap, _ = ae.snapshot(args, NOW + 86400 + 1, state2)
    assert snap["agents"]["codex"]["stale"] == 1
