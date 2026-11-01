#!/usr/bin/env bash
# Gate: Beszel's own custom-metrics code (henrygd/beszel#2562) must read our output cleanly.
# Writes a fresh .prom from this machine, then runs Beszel's parser and collector on it with
# beszel/config.yml. Needs git and Go. Exit 0 = Beszel reads every series with units and names.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
work=${BESZEL_CHECK_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/agent-exporter/beszel-check}
ref=${BESZEL_REF:-pull/2562/head}
mkdir -p "$work"
if [ ! -d "$work/beszel/.git" ]; then
  git clone -q --depth 50 https://github.com/henrygd/beszel.git "$work/beszel"
fi
git -C "$work/beszel" fetch -q origin "$ref"
git -C "$work/beszel" checkout -q --detach FETCH_HEAD
# Live numbers from this machine, plus one known waiting session, so the per-session key that
# BeszelBar parses always goes through Beszel's parser.
python3 - "$here" "$work" <<'PY'
import os, sys, time
sys.path.insert(0, sys.argv[1])
import agent_exporter as ae
work = sys.argv[2]
args = ae.build_parser().parse_args(["collect", "--runtime-dir", os.path.join(work, "rt")])
snap, _ = ae.snapshot(args, time.time(), {})
snap["waiting"] = [{"name": "beszel-check", "seconds": 2700, "session_id": "check"}] + snap["waiting"]
ae.write_atomic(os.path.join(work, "prom", "agents.prom"), ae.render_prom(snap))
PY
mkdir -p "$work/data"
sed "s|/home/YOU/.local/state/agent-exporter/agents.prom|$work/prom/agents.prom|" \
  "$here/beszel/config.yml" > "$work/data/config.yml"
pkg="$work/beszel/agent/custommetrics"
test_file="$pkg/zz_agent_exporter_check_test.go"
trap 'rm -f "$test_file"' EXIT
sed -e "s|@PROM@|$work/prom/agents.prom|" -e "s|@DATA@|$work/data|" \
  "$here/scripts/beszel_check_test.go.tmpl" > "$test_file"
(cd "$pkg" && go test -count=1 -run TestAgentExporterFile .)
