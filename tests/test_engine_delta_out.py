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


def test_empty_monitor_normalized_not_stranded(tmp_path):
    # A whitespace monitor collapses to None, so the pairing guard fires cleanly
    # instead of tagging an unleased empty-key run (which could strand below a
    # concurrent empty-key run's watermark).
    proc = _run(["topic", "--mock", "--store", "--monitor", "   ",
                 "--delta-out", str(tmp_path / "d.json"), "--emit", "compact"],
                tmp_path)
    assert proc.returncode == 2
    assert "--delta-out requires --monitor" in proc.stderr


def test_monitor_requires_delta_out(tmp_path):
    # A monitor run without --delta-out would skip the lease and could strand a
    # run below a later acked watermark; the pairing is rejected.
    proc = _run(["topic", "--mock", "--store", "--monitor", "m1",
                 "--emit", "compact"], tmp_path)
    assert proc.returncode != 0
    assert "--monitor requires --delta-out" in proc.stderr


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


def test_baseline_self_anchors_no_second_baseline(tmp_path):
    # #3 overlap fix: a baseline run durably anchors its own watermark, so a
    # second run computes a real delta instead of also classifying as baseline
    # and losing its findings — even if the agent never acked the baseline.
    out1 = tmp_path / "d1.json"
    r1 = _run(["alpha", "--mock", "--store", "--monitor", "m1",
               "--delta-out", str(out1), "--emit", "compact"], tmp_path)
    assert r1.returncode == 0, r1.stderr
    d1 = json.loads(out1.read_text())
    assert d1["status"] == "baseline"
    # Re-acking the self-anchored baseline is an idempotent success, not a refusal.
    ack = _run(["monitor-ack", "--monitor", "m1", "--ack-run", str(d1["run_id"])],
               tmp_path)
    assert ack.returncode == 0, ack.stderr
    out2 = tmp_path / "d2.json"
    r2 = _run(["alpha", "--mock", "--store", "--monitor", "m1",
               "--delta-out", str(out2), "--emit", "compact"], tmp_path)
    assert r2.returncode == 0, r2.stderr
    assert json.loads(out2.read_text())["status"] == "ok"  # not baseline again


def test_monitor_ack_requires_args(tmp_path):
    proc = _run(["monitor-ack", "--monitor", "m1"], tmp_path)
    assert proc.returncode == 2


def test_delta_out_implies_store(tmp_path):
    # No --store: --delta-out must still persist + write the delta, not run and
    # silently produce no file.
    out = tmp_path / "d.json"
    proc = _run(["alpha", "--mock", "--monitor", "m1",
                 "--delta-out", str(out), "--emit", "compact"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    assert json.loads(out.read_text())["monitor"] == "m1"


def test_monitor_ack_rejects_bad_run_id(tmp_path):
    proc = _run(["monitor-ack", "--monitor", "m1", "--ack-run", "999999"], tmp_path)
    assert proc.returncode == 3
    assert "refused" in proc.stderr


def test_monitor_reset_is_wired_and_cas_safe(tmp_path):
    # The subcommand runs, derives the topic, and is a CAS no-op on a VALID
    # watermark: a straggler reset must not clear a live watermark (which would
    # re-baseline and suppress fresh findings). The missing->clear path is
    # unit-tested in test_store_delta_watermark.
    out1 = tmp_path / "d1.json"
    r1 = _run(["alpha", "--mock", "--store", "--monitor", "m1",
               "--delta-out", str(out1), "--emit", "compact"], tmp_path)
    assert r1.returncode == 0, r1.stderr
    run_id = json.loads(out1.read_text())["run_id"]  # baseline self-anchors wm
    reset = _run(["monitor-reset", "--monitor", "m1", "--ack-run", str(run_id)],
                 tmp_path)
    assert reset.returncode == 0 and "reset" in reset.stdout
    out2 = tmp_path / "d2.json"
    r2 = _run(["alpha", "--mock", "--store", "--monitor", "m1",
               "--delta-out", str(out2), "--emit", "compact"], tmp_path)
    assert r2.returncode == 0, r2.stderr
    assert json.loads(out2.read_text())["status"] == "ok"  # watermark preserved
