#!/bin/sh
# Writes memory pressure as a Beszel custom metric (henrygd/beszel#2562):
#   memory_pressure_level 0 = normal, 1 = warning, 2 = critical.
# Linux reads PSI from /proc/pressure/memory. Warning: some task stalled on memory for at least
# 10% of the last 10 s. Critical: every task stalled for at least 10% of it.
# macOS reads the kernel's own level, the one Activity Monitor colours (1, 2 and 4).
#
# Usage: mem-pressure.sh OUT.prom [interval-seconds]   (default 10; 0 writes once and exits)
#        mem-pressure.sh - 0                            (print once to stdout)
# Env for tests: PSI_FILE (default /proc/pressure/memory), MAC_LEVEL (skips sysctl).
set -eu

out=${1:?usage: mem-pressure.sh OUT.prom|- [interval-seconds]}
interval=${2:-10}
psi=${PSI_FILE:-/proc/pressure/memory}

level() {
    if [ -r "$psi" ]; then
        awk '/^some/ { split($2, a, "="); s = a[2] }
             /^full/ { split($2, a, "="); f = a[2] }
             END { if (s == "") exit 1; print (f + 0 >= 10) ? 2 : (s + 0 >= 10) ? 1 : 0 }' "$psi"
    else
        case ${MAC_LEVEL:-$(sysctl -n kern.memorystatus_vm_pressure_level)} in
            4) echo 2 ;;
            2) echo 1 ;;
            1) echo 0 ;;
            *) echo "mem-pressure: unknown macOS level" >&2; return 1 ;;
        esac
    fi
}

render() {
    printf '# HELP memory_pressure_level Memory pressure: 0 normal, 1 warning, 2 critical.\n'
    printf '# TYPE memory_pressure_level gauge\n'
    printf 'memory_pressure_level %s\n' "$1"
}

while :; do
    l=$(level)
    if [ "$out" = - ]; then
        render "$l"
    else
        # Beszel skips names that do not end in .prom, so the rename is atomic for it.
        render "$l" > "$out.tmp"
        mv "$out.tmp" "$out"
    fi
    [ "$interval" = 0 ] && exit 0
    sleep "$interval"
done
