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
git -C "$work/beszel" fetch -q origin "$ref:check-ref" --force
git -C "$work/beszel" checkout -q check-ref
python3 "$here/agent_exporter.py" collect --runtime-dir "$work/rt" --out "$work/prom/agents.prom"
mkdir -p "$work/data"
sed "s|/home/YOU/.local/state/agent-exporter/agents.prom|$work/prom/agents.prom|" \
  "$here/beszel/config.yml" > "$work/data/config.yml"
pkg="$work/beszel/agent/custommetrics"
test_file="$pkg/zz_agent_exporter_check_test.go"
trap 'rm -f "$test_file"' EXIT
sed -e "s|@PROM@|$work/prom/agents.prom|" -e "s|@DATA@|$work/data|" \
  "$here/scripts/beszel_check_test.go.tmpl" > "$test_file"
(cd "$pkg" && go test -count=1 -run TestAgentExporterFile .)
