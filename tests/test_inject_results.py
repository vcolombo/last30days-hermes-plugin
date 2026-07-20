"""Tests for --plan-queries and --inject-results (Hermes two-phase inject)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "skills" / "last30days" / "scripts" / "last30days.py"
SCRIPTS = ROOT / "skills" / "last30days" / "scripts"


def _run_plan_queries(tmp_path, *extra):
    out = tmp_path / "plan.json"
    proc = subprocess.run(
        [sys.executable, str(ENGINE), "test topic", "--plan-queries",
         "--plan-queries-out", str(out), "--mock", *extra],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


class TestPlanQueries:
    def test_emits_payload_shape(self, tmp_path):
        payload = _run_plan_queries(tmp_path)
        assert payload["topic"] == "test topic"
        assert payload["depth"] == "default"
        assert payload["from_date"] < payload["to_date"]
        assert isinstance(payload["plan"], dict)
        assert payload["plan"]["subqueries"]
        sources = {q["source"] for q in payload["queries"]}
        assert sources <= {"x", "web"}
        assert "x" in sources and "web" in sources
        ids = [q["id"] for q in payload["queries"]]
        assert len(ids) == len(set(ids))

    def test_x_queries_respect_fetch_cap(self, tmp_path):
        payload = _run_plan_queries(tmp_path)
        x_queries = [q for q in payload["queries"] if q["source"] == "x"]
        assert 1 <= len(x_queries) <= 2  # MAX_SOURCE_FETCHES["x"]

    def test_plan_round_trips_through_sanitizer(self, tmp_path):
        payload = _run_plan_queries(tmp_path)
        sys.path.insert(0, str(SCRIPTS))
        try:
            from lib import planner
        finally:
            sys.path.remove(str(SCRIPTS))
        plan = planner._sanitize_plan(
            payload["plan"], payload["topic"],
            ["x", "grounding", "reddit", "hackernews"], None, payload["depth"],
        )
        # Sanitize must be idempotent on its own output: the exact
        # search_query strings are the injection keys in phase 3.
        emitted = {q["search_query"] for q in payload["queries"]}
        round_tripped = {sq.search_query for sq in plan.subqueries}
        assert emitted <= round_tripped


X_ITEM = {
    "id": "X1", "text": "great post about test topic",
    "url": "https://x.com/someone/status/123456", "author_handle": "someone",
    "date": "2026-07-01",
    "engagement": {"likes": 100, "reposts": 25, "replies": 15, "quotes": 5},
    "why_relevant": "on topic", "relevance": 0.9,
}
WEB_ITEM = {
    "id": "WI1", "title": "Test topic roundup", "url": "https://example.com/a",
    "source_domain": "example.com", "snippet": "all about test topic",
    "date": "2026-07-02", "relevance": 0.8, "why_relevant": "hermes web_search",
}


class TestInjectResults:
    def _pipeline(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            from lib import pipeline
        finally:
            sys.path.remove(str(SCRIPTS))
        return pipeline

    def test_injected_results_membership_semantics(self):
        pipeline = self._pipeline()
        config = {"_inject_results": {"x": {"q1": [X_ITEM], "q2": []}, "web": {}}}
        assert pipeline._injected_results(config, "x", "q1") == [X_ITEM]
        # Empty list is a HIT (zero results), not a miss.
        assert pipeline._injected_results(config, "x", "q2") == []
        # Absent key is a miss.
        assert pipeline._injected_results(config, "x", "q3") is None
        # No injection configured at all.
        assert pipeline._injected_results({}, "x", "q1") is None

    def test_end_to_end_inject_round_trip(self, tmp_path, monkeypatch):
        """Plan queries, inject fixtures for them, run phase 3, and assert the
        injected URLs surface in the rendered output with no live X/web fetch."""
        payload = _run_plan_queries(tmp_path)
        inject = {"x": {}, "web": {}}
        for q in payload["queries"]:
            bucket = "x" if q["source"] == "x" else "web"
            inject[bucket][q["search_query"]] = (
                [X_ITEM] if bucket == "x" else [WEB_ITEM])
        inject_file = tmp_path / "inject.json"
        inject_file.write_text(json.dumps(inject), encoding="utf-8")
        plan_file = tmp_path / "plan-only.json"
        plan_file.write_text(json.dumps(payload["plan"]), encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(ENGINE), "test topic",
             "--plan", str(plan_file),
             "--inject-results", str(inject_file),
             "--mock", "--emit", "compact"],
            capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        # Mock mode keeps every other source offline; the injected items are
        # only reachable through the injection seam.
        assert "x.com/someone/status/123456" in proc.stdout or \
               "example.com/a" in proc.stdout

    def test_inject_mode_forces_x_available_without_credentials(self, tmp_path):
        """Injected results must reach the seam even when the engine env has
        no X/web credentials (OAuth-only Hermes hosts). Without the inject-mode
        availability force-add, _sanitize_plan strips x from every subquery and
        the injected items are silently discarded. Runs WITHOUT --mock so real
        availability logic applies; zero network because every x query is
        injected, grounding is disabled, and other sources are excluded."""
        payload = _run_plan_queries(tmp_path)
        inject = {"x": {}, "web": {}}
        for q in payload["queries"]:
            if q["source"] == "x":
                inject["x"][q["search_query"]] = [X_ITEM]
        inject_file = tmp_path / "inject.json"
        inject_file.write_text(json.dumps(inject), encoding="utf-8")
        plan_file = tmp_path / "plan-only.json"
        plan_file.write_text(json.dumps(payload["plan"]), encoding="utf-8")

        import os
        env = {"PATH": os.environ.get("PATH", ""),
               "HOME": str(tmp_path),  # no user config, no keys
               "LAST30DAYS_CONFIG_DIR": ""}
        proc = subprocess.run(
            [sys.executable, str(ENGINE), "test topic",
             "--plan", str(plan_file),
             "--inject-results", str(inject_file),
             "--search", "x", "--web-backend", "none",
             "--emit", "compact"],
            capture_output=True, text=True, timeout=180, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "x.com/someone/status/123456" in proc.stdout

    def test_inject_miss_is_quiet_no_coverage(self, tmp_path):
        """A subquery not present in the inject map must not raise and must
        not fall through to live backends (injected-only policy)."""
        payload = _run_plan_queries(tmp_path)
        inject_file = tmp_path / "inject.json"
        inject_file.write_text(json.dumps({"x": {}, "web": {}}), encoding="utf-8")
        plan_file = tmp_path / "plan-only.json"
        plan_file.write_text(json.dumps(payload["plan"]), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ENGINE), "test topic",
             "--plan", str(plan_file),
             "--inject-results", str(inject_file),
             "--mock", "--emit", "compact"],
            capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
