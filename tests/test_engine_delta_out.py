"""Engine --monitor/--delta-out + the monitor-ack subcommand (scheduled
monitoring). Subprocesses run with HOME pinned to a tmp dir so store.py's
Path.home()-scoped research.db is isolated per test."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "skills" / "last30days" / "scripts" / "last30days.py"


def _run(args, cwd):
    env = {**os.environ, "HOME": str(cwd)}
    return subprocess.run([sys.executable, str(ENGINE), *args],
                          capture_output=True, text=True, timeout=180,
                          cwd=cwd, env=env)


def test_delta_out_requires_monitor(tmp_path):
    proc = _run(["topic", "--mock", "--store", "--delta-out",
                 str(tmp_path / "d.json"), "--emit", "compact"], tmp_path)
    assert proc.returncode != 0
    assert "--delta-out requires --monitor" in proc.stderr


def test_first_run_writes_baseline_delta(tmp_path):
    out = tmp_path / "d.json"
    proc = _run(["alpha", "--mock", "--store", "--monitor", "m1",
                 "--delta-out", str(out), "--emit", "compact"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    delta = json.loads(out.read_text())
    assert delta["schema"] == "delta.v1"
    assert delta["monitor"] == "m1"
    assert delta["status"] == "baseline"
    assert isinstance(delta["run_id"], int)
    assert (out.stat().st_mode & 0o777) == 0o600
