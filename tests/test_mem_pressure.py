"""mem-pressure.sh: PSI and macOS levels map to 0 normal, 1 warning, 2 critical."""
import os
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "mem-pressure.sh")


def run(tmp_path, *, psi=None, mac=None, args=("-", "0")):
    env = {"PATH": os.environ["PATH"], "PSI_FILE": str(tmp_path / "none")}
    if psi is not None:
        (tmp_path / "psi").write_text(psi)
        env["PSI_FILE"] = str(tmp_path / "psi")
    if mac is not None:
        env["MAC_LEVEL"] = str(mac)
    return subprocess.run(["sh", SCRIPT, *args], env=env, capture_output=True, text=True)


def psi(some, full):
    return (f"some avg10={some} avg60=0.00 avg300=0.00 total=1\n"
            f"full avg10={full} avg60=0.00 avg300=0.00 total=1\n")


@pytest.mark.parametrize("some,full,level", [
    ("0.00", "0.00", 0), ("9.99", "9.99", 0), ("10.00", "0.00", 1),
    ("45.10", "9.00", 1), ("60.00", "10.00", 2),
])
def test_linux_levels(tmp_path, some, full, level):
    r = run(tmp_path, psi=psi(some, full))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == f"memory_pressure_level {level}"
    assert "# TYPE memory_pressure_level gauge" in r.stdout


@pytest.mark.parametrize("mac,level", [(1, 0), (2, 1), (4, 2)])
def test_mac_levels(tmp_path, mac, level):
    r = run(tmp_path, mac=mac)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == f"memory_pressure_level {level}"


def test_unknown_mac_level_fails(tmp_path):
    assert run(tmp_path, mac=7).returncode != 0


def test_unreadable_psi_line_fails(tmp_path):
    assert run(tmp_path, psi="garbage\n").returncode != 0


def test_writes_file_atomically(tmp_path):
    out = tmp_path / "mem.prom"
    r = run(tmp_path, psi=psi("0.00", "0.00"), args=(str(out), "0"))
    assert r.returncode == 0, r.stderr
    assert out.read_text().endswith("memory_pressure_level 0\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mem.prom", "psi"]
