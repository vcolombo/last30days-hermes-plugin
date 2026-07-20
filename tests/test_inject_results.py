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

    def test_phase2_supplemental_skipped_in_inject_mode(self, monkeypatch):
        """Injected-only mode must skip Phase 2 supplemental X lanes entirely:
        they are live credentialed fetches with no injection seam. Without the
        guard, x_handle drives entity extraction and the handle/mention lanes."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            from lib import pipeline, schema
        finally:
            sys.path.remove(str(SCRIPTS))
        import threading

        def boom(*args, **kwargs):
            raise AssertionError("entity extraction must not run in inject mode")

        monkeypatch.setattr(pipeline.entity_extract, "extract_entities", boom)
        plan = schema.QueryPlan(
            intent="general",
            freshness_mode="balanced_recent",
            cluster_mode="story",
            raw_topic="test topic",
            subqueries=[],
            source_weights={},
        )
        result = pipeline._run_supplemental_searches(
            topic="test topic",
            bundle=schema.RetrievalBundle(),
            plan=plan,
            config={"_inject_results": {"x": {}, "web": {}}},
            depth="default",
            date_range=("2026-06-20", "2026-07-20"),
            runtime=None,  # guard returns before runtime is touched
            mock=False,
            rate_limited_sources=set(),
            rate_limit_lock=threading.Lock(),
            x_handle="someone",
        )
        assert result is None

    def test_resolve_x_backend_is_local_only_in_inject_mode(self, monkeypatch):
        """providers._resolve_x_backend must not run xurl's live `whoami`
        probe when the engine is in injected-only mode."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            from lib import providers
        finally:
            sys.path.remove(str(SCRIPTS))
        calls = []

        def spy(config, local_only=False):
            calls.append(local_only)
            return None

        monkeypatch.setattr(providers.env, "get_x_source", spy)
        providers._resolve_x_backend({"_inject_results": {"x": {}, "web": {}}})
        providers._resolve_x_backend({})
        assert calls == [True, False]

    def test_inject_mode_never_spawns_xurl(self, tmp_path):
        """Behavioral no-network guarantee: a fake `xurl` shim earlier on PATH
        writes a marker file when executed. The credential-less inject run must
        never execute it — availability resolves from local evidence only."""
        import os
        payload = _run_plan_queries(tmp_path)
        inject = {"x": {}, "web": {}}
        for q in payload["queries"]:
            if q["source"] == "x":
                inject["x"][q["search_query"]] = [X_ITEM]
        inject_file = tmp_path / "inject.json"
        inject_file.write_text(json.dumps(inject), encoding="utf-8")
        plan_file = tmp_path / "plan-only.json"
        plan_file.write_text(json.dumps(payload["plan"]), encoding="utf-8")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "xurl-invoked"
        shim = bin_dir / "xurl"
        shim.write_text(
            f"#!/bin/sh\ntouch {marker}\n"
            'echo \'{"data":{"username":"fake"}}\'\nexit 0\n',
            encoding="utf-8")
        shim.chmod(0o755)

        env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
               "HOME": str(tmp_path),  # no user config, no keys, no token store
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
        assert not marker.exists(), "inject mode executed xurl (live probe)"
        assert "x.com/someone/status/123456" in proc.stdout

    def test_plan_queries_mode_never_spawns_xurl(self, tmp_path):
        """Phase 1 (--plan-queries) never fetches X — the plugin fetches via
        Hermes — so it must not probe X backends live either. Same fake-xurl
        shim guarantee as the inject-mode test, but for the plan phase and
        WITHOUT --mock so real availability/backend resolution runs."""
        import os
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "xurl-invoked"
        shim = bin_dir / "xurl"
        shim.write_text(
            f"#!/bin/sh\ntouch {marker}\n"
            'echo \'{"data":{"username":"fake"}}\'\nexit 0\n',
            encoding="utf-8")
        shim.chmod(0o755)

        out = tmp_path / "plan.json"
        env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
               "HOME": str(tmp_path),  # no user config, no keys, no token store
               "LAST30DAYS_CONFIG_DIR": ""}
        proc = subprocess.run(
            [sys.executable, str(ENGINE), "test topic", "--plan-queries",
             "--plan-queries-out", str(out),
             "--search", "x", "--web-backend", "none"],
            capture_output=True, text=True, timeout=180, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert not marker.exists(), "plan-queries mode executed xurl (live probe)"
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["queries"]

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
