"""Behaviour regression tests for two-phase inject credential isolation.

These cover the paths that reach a live X/web evidence backend *outside* the
pipeline seam — competitor peer resolution, and a malformed injection — which a
predicate consolidation alone does not gate. Each was a real gap (Codex + Benson
review of PR #8): the seam lint bans re-scattering the *predicate spelling*, but
cannot catch a *missing* gate, so these behaviour tests stand in for it.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "skills" / "last30days" / "scripts" / "last30days.py"


def _run(args, tmp_path):
    return subprocess.run(
        [sys.executable, str(ENGINE), *args],
        capture_output=True, text=True, timeout=180, cwd=tmp_path)


class TestCompetitorInjectedIsolation:
    def test_vs_topic_in_injected_mode_skips_live_peer_resolution(self, tmp_path):
        """A normal comparison topic ("A vs B") auto-enters vs-mode with no
        --competitors flag. In injected mode the per-peer auto_resolve must be
        skipped (it would hit a live web backend the host owns), and the run
        must still complete. This is the product-path leak Benson caught."""
        inject = tmp_path / "inject.json"
        inject.write_text(json.dumps({"x": {}, "web": {}}), encoding="utf-8")
        proc = _run(
            ["alpha vs beta", "--inject-results", str(inject),
             "--mock", "--emit", "compact"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        # The gate fired for the peer (proves MY gate, not the --mock skip:
        # without the gate, mock would skip silently with no such line).
        assert "injected mode: skipping live peer resolution for 'beta'" \
            in proc.stderr, proc.stderr
        # And no live peer resolution was attempted.
        assert "auto_resolve failed" not in proc.stderr


class TestInjectValidation:
    def test_json_null_inject_fails_closed(self, tmp_path):
        """A JSON `null` injection would store _inject_results=None → is_injected
        False → a silently un-isolated live pass. Must fail closed, not run."""
        inject = tmp_path / "null.json"
        inject.write_text("null", encoding="utf-8")
        proc = _run(
            ["topic", "--inject-results", str(inject), "--emit", "compact"],
            tmp_path)
        assert proc.returncode == 2, (proc.returncode, proc.stderr)
        assert "must be a JSON object" in proc.stderr

    def test_json_list_inject_fails_closed(self, tmp_path):
        inject = tmp_path / "list.json"
        inject.write_text("[]", encoding="utf-8")
        proc = _run(
            ["topic", "--inject-results", str(inject), "--emit", "compact"],
            tmp_path)
        assert proc.returncode == 2, (proc.returncode, proc.stderr)
        assert "must be a JSON object" in proc.stderr

    def test_valid_dict_inject_is_accepted(self, tmp_path):
        """The happy path still works — an empty dict is a valid zero-result
        injection, not a malformed one."""
        inject = tmp_path / "ok.json"
        inject.write_text(json.dumps({"x": {}, "web": {}}), encoding="utf-8")
        proc = _run(
            ["topic", "--inject-results", str(inject), "--mock",
             "--emit", "compact"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "must be a JSON object" not in proc.stderr
