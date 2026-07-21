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


def test_monitor_ack_sets_watermark(tmp_path):
    out1 = tmp_path / "d1.json"
    r1 = _run(["alpha", "--mock", "--store", "--monitor", "m1",
               "--delta-out", str(out1), "--emit", "compact"], tmp_path)
    assert r1.returncode == 0, r1.stderr
    run_id = json.loads(out1.read_text())["run_id"]

    ack = _run(["monitor-ack", "--monitor", "m1", "--ack-run", str(run_id)], tmp_path)
    assert ack.returncode == 0, ack.stderr
    assert "acked" in ack.stdout

    out2 = tmp_path / "d2.json"
    r2 = _run(["alpha", "--mock", "--store", "--monitor", "m1",
               "--delta-out", str(out2), "--emit", "compact"], tmp_path)
    assert r2.returncode == 0, r2.stderr
    delta2 = json.loads(out2.read_text())
    assert delta2["status"] == "ok"                # not baseline anymore
    assert delta2["previous_run_id"] == run_id


def test_monitor_ack_requires_args(tmp_path):
    proc = _run(["monitor-ack", "--monitor", "m1"], tmp_path)
    assert proc.returncode == 2
