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
