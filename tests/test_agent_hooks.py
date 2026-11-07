"""Codex, Gemini CLI and OpenCode hooks: their events map to the same states as Claude Code's."""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_exporter as ae  # noqa: E402


@pytest.mark.parametrize("event,payload,expected", [
    # Codex: Claude Code's names and fields.
    ("UserPromptSubmit", {}, ("working", None)),
    ("PermissionRequest", {"tool_name": "shell"}, ("waiting", "permission")),
    ("PostToolUse", {"tool_name": "shell"}, ("working", None)),
    ("Stop", {}, ("idle", None)),
    ("SessionEnd", {}, ("ended", None)),
    # Gemini CLI.
    ("BeforeAgent", {"prompt": "x"}, ("working", None)),
    ("BeforeTool", {"tool_name": "ask_user"}, ("waiting", "question")),
    ("BeforeTool", {"tool_name": "exit_plan_mode"}, ("waiting", "plan")),
    ("BeforeTool", {"tool_name": "run_shell_command"}, None),
    ("Notification", {"notification_type": "ToolPermission"}, ("waiting", "permission")),
    ("AfterTool", {"tool_name": "run_shell_command"}, ("working", None)),
    ("AfterAgent", {}, ("idle", None)),
    # OpenCode, through the plugin.
    ("session.status", {"status": "busy"}, ("working", None)),
    ("session.status", {"status": "retry"}, ("working", None)),
    ("session.status", {"status": "idle"}, ("idle", None)),
    ("session.status", {}, None),
    ("permission.asked", {}, ("waiting", "permission")),
    ("question.asked", {}, ("waiting", "question")),
    ("permission.replied", {}, ("working", None)),
    ("question.rejected", {}, ("working", None)),
    ("session.error", {}, ("idle", None)),
    ("session.deleted", {}, ("ended", None)),
])
def test_other_agents_events(event, payload, expected):
    assert ae.hook_state(event, payload) == expected


def run_cli(*argv):
    out = subprocess.run([sys.executable, ae.__file__, *argv], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_codex_hooks_run_in_turn_and_ask_for_trust(tmp_path):
    path = tmp_path / "hooks.json"
    r = run_cli("install-hooks", "--agent", "codex", "--settings", str(path), "--hook-command", "/x/agent-exporter hook", "--json")
    assert r["agent"] == "codex" and r["changed"] == list(ae.CODEX_HOOK_EVENTS) and "/hooks" in r["next"]
    data = json.loads(path.read_text())
    assert set(data) == {"hooks"}  # Codex rejects unknown top-level keys
    handler = data["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert handler == {"type": "command", "command": "/x/agent-exporter hook", "timeout": 10}  # Codex skips async hooks
    again = run_cli("install-hooks", "--agent", "codex", "--settings", str(path), "--hook-command", "/x/agent-exporter hook", "--json")
    assert again["changed"] == [] and "next" not in again
    run_cli("uninstall-hooks", "--agent", "codex", "--settings", str(path), "--json")
    assert json.loads(path.read_text()) == {}


def test_gemini_hooks_keep_settings_and_use_milliseconds(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"security": {"auth": {"selectedType": "vertex-ai"}}}))
    run_cli("install-hooks", "--agent", "gemini", "--settings", str(path), "--hook-command", "/x/agent-exporter hook", "--json")
    data = json.loads(path.read_text())
    assert data["security"] == {"auth": {"selectedType": "vertex-ai"}}
    assert data["hooks"]["BeforeTool"][0]["matcher"] == "ask_user|exit_plan_mode"
    assert data["hooks"]["AfterAgent"][0]["hooks"][0] == {"type": "command", "command": "/x/agent-exporter hook", "timeout": 10000}
    run_cli("uninstall-hooks", "--agent", "gemini", "--settings", str(path), "--json")
    assert json.loads(path.read_text()) == {"security": {"auth": {"selectedType": "vertex-ai"}}}


@pytest.mark.parametrize("agent", ["claude", "codex", "gemini"])
def test_hooks_keep_a_private_settings_file_private(tmp_path, agent):
    # Issue #1: the rewrite used to come back 0644 whatever the file's mode was.
    path = tmp_path / "settings.json"
    path.write_text("{}\n")
    path.chmod(0o600)
    for cmd in ("install-hooks", "uninstall-hooks"):
        run_cli(cmd, "--agent", agent, "--settings", str(path), "--hook-command", "/x/agent-exporter hook", "--json")
        assert path.stat().st_mode & 0o777 == 0o600, cmd


def test_write_atomic_new_file_gets_the_umask_default(tmp_path):
    old = os.umask(0o022)
    try:
        ae.write_atomic(str(tmp_path / "new.json"), "{}\n")
    finally:
        os.umask(old)
    assert (tmp_path / "new.json").stat().st_mode & 0o777 == 0o644


def test_opencode_plugin_install_is_idempotent_and_never_overwrites_a_foreign_file(tmp_path):
    path = tmp_path / "plugins" / "agent-exporter.js"
    r = ae.edit_plugin(str(path), True, "/x/agent-exporter hook", False)
    assert r["changed"] == ["plugin"] and '"/x/agent-exporter hook"' in path.read_text()
    assert ae.edit_plugin(str(path), True, "/x/agent-exporter hook", False)["changed"] == []
    ae.edit_plugin(str(path), False, "", False)
    assert not path.exists()
    path.write_text("export const Mine = async () => ({})\n")
    with pytest.raises(SystemExit):
        ae.edit_plugin(str(path), True, "/x/agent-exporter hook", False)
    assert path.read_text() == "export const Mine = async () => ({})\n"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_opencode_plugin_sends_main_session_events_only(tmp_path):
    """Load the real plugin in node and feed it OpenCode's events; the command gets one JSON line each."""
    out = tmp_path / "events"
    out.mkdir()
    plugin = tmp_path / "agent-exporter.mjs"
    ae.edit_plugin(str(plugin), True, f"cat > $(mktemp -p {out})", False)  # one file per event
    driver = tmp_path / "drive.mjs"
    driver.write_text(f"""
import {{ AgentExporter }} from {json.dumps(str(plugin))}
const hooks = await AgentExporter({{ directory: "/work/app" }})
const feed = (type, properties) => hooks.event({{ event: {{ type, properties }} }})
await feed("session.created", {{ info: {{ id: "ses_child", parentID: "ses_main" }} }})
await feed("session.status", {{ sessionID: "ses_main", status: {{ type: "busy" }} }})
await feed("session.status", {{ sessionID: "ses_child", status: {{ type: "idle" }} }})
await feed("message.updated", {{ sessionID: "ses_main" }})
await feed("permission.asked", {{ id: "per_1", sessionID: "ses_main" }})
await new Promise(r => setTimeout(r, 800))
""")
    subprocess.run(["node", str(driver)], check=True, timeout=30)
    lines = [json.loads(f.read_text()) for f in out.iterdir()]
    got = sorted((e["hook_event_name"], e.get("status")) for e in lines)
    assert got == [("permission.asked", None), ("session.status", "busy")], lines
    assert {e["session_id"] for e in lines} == {"ses_main"} and {e["cwd"] for e in lines} == {"/work/app"}
    assert {e["agent"] for e in lines} == {"opencode"} and all(isinstance(e["agent_pid"], int) for e in lines)
