# Hermes Agent Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package this repo as a native Hermes Agent plugin whose `last30days_research` tool routes X + web fetches through the agent's own `x_search`/`web_search` tools (via `ctx.dispatch_tool`), so no separate credentials are needed under Hermes.

**Architecture:** Two-phase inject. Phase 1: engine subprocess plans queries (`--plan-queries`) and writes them + the serialized plan to a JSON file. Phase 2: plugin handler fetches each query via `ctx.dispatch_tool` and adapts results to the engine's existing raw item shapes. Phase 3: engine subprocess re-runs with `--plan` (round-tripped plan) + `--inject-results` (fetched items); X/grounding branches consume injected results (injected-only — no live fallback); all other sources fetch as today.

**Tech Stack:** Python stdlib only (engine + plugin). Spec: `docs/superpowers/specs/2026-07-20-hermes-plugin-design.md`.

## Global Constraints

- Python 3.12+ engine; Hermes runtime is Python 3.13.5 with NO `python3.12` binary — plugin subprocesses must use `sys.executable`.
- `plugin.yaml` `name:` must be exactly `last30days`; `version:` must equal `pyproject.toml [project].version` (currently `3.16.0`) — enforced by the version-lockstep test added in Task 4.
- `requires_env: []` — the plugin itself needs no credentials.
- Every new `lib/*.py` call to `log.source_log(...)` must pass `tty_only=False` (AGENTS.md rule, enforced by `tests/test_source_log_visibility.py`).
- `skills/last30days/scripts/lib/__init__.py` stays a bare package marker.
- Do not touch SKILL.md Step 0 onboarding (locked by `tests/test_onboarding_contract.py`).
- Do not lower `fail_under` in `pyproject.toml`.
- No AI attribution in commits (user's global CLAUDE.md).
- Injected-only policy: when `--inject-results` is active, X and grounding NEVER fall through to live credentialed backends; misses are quiet no-coverage.
- Injection map is source-qualified and matched membership-based (`query in bucket`), so `[]` is a zero-result hit, not a miss.
- Run tests with `uv run pytest ...` from the repo root.

---

### Task 1: Engine `--plan-queries` / `--plan-queries-out`

**Files:**
- Modify: `skills/last30days/scripts/lib/pipeline.py` (`run()` signature ~:1252, availability block ~:1321-1326, return seam just before `bundle = schema.RetrievalBundle(...)` ~:1412)
- Modify: `skills/last30days/scripts/last30days.py` (argparse near `--plan` at :580; research path just before `comp_enabled, comp_count, comp_explicit = resolve_competitors_args(args)` ~:2399)
- Test: `tests/test_inject_results.py` (new)

**Interfaces:**
- Produces: `pipeline.run(..., plan_queries_only: bool = False)` — when True, returns `schema.QueryPlan` instead of `schema.Report`, immediately after planning (nothing fetches).
- Produces: CLI `last30days.py <topic> --plan-queries [--plan-queries-out FILE]` writing JSON:
  ```json
  {"topic": "...", "depth": "default", "from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD",
   "plan": {"...": "schema.to_dict(plan) — feed back verbatim via --plan"},
   "queries": [{"id": "x1", "source": "x", "search_query": "..."},
                {"id": "w1", "source": "web", "search_query": "..."}]}
  ```
- Consumed by: Task 3 handler (reads the file), Task 2 (`--inject-results` keys are the `search_query` strings from this payload, bucketed by source).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inject_results.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_inject_results.py -v`
Expected: FAIL — `--plan-queries` unknown argument (returncode 2, argparse error in stderr).

- [ ] **Step 3: Implement `plan_queries_only` in `pipeline.run()`**

In `skills/last30days/scripts/lib/pipeline.py`:

1. Add to the `run()` keyword args (after `corpus_all_time: bool = False,`):

```python
    plan_queries_only: bool = False,
) -> schema.Report | schema.QueryPlan:
```

(return annotation currently `-> schema.Report:` — widen it.)

2. After the `web_backend`/grounding availability adjustments (directly below the `elif web_backend in (...)` block at ~:1323-1324), add:

```python
    if plan_queries_only:
        # Two-phase hosts (Hermes plugin) fetch X/web themselves via the
        # agent's own tools, so the planner must assign those queries even
        # when this environment has no X/web credentials.
        for source in ("x", "grounding"):
            if source not in available:
                available.append(source)
```

3. Immediately after the planner-trace block (after the `print("[Planner]   (no subqueries in plan)", ...)` else-branch at ~:1410, BEFORE `bundle = schema.RetrievalBundle(...)`), add:

```python
    if plan_queries_only:
        return plan
```

- [ ] **Step 4: Implement the CLI flags and branch in `last30days.py`**

1. Next to the `--plan` argument (:580), add:

```python
    parser.add_argument("--plan-queries", action="store_true",
                        help="Plan only: write the X/web queries this run would execute as JSON "
                             "and exit without fetching. Single-entity topics only (competitors/"
                             "vs-mode unsupported). Used by two-phase hosts (e.g. the Hermes plugin).")
    parser.add_argument("--plan-queries-out",
                        help="File path for --plan-queries JSON output (default: stdout)")
```

2. In the research path, immediately BEFORE `comp_enabled, comp_count, comp_explicit = resolve_competitors_args(args)` (~:2399) — after `external_plan` parsing and auto-resolve so those still apply — add:

```python
        if args.plan_queries:
            plan = pipeline.run(
                topic=topic,
                config=config,
                depth=depth,
                requested_sources=requested_sources,
                mock=args.mock,
                x_handle=args.x_handle,
                x_related=x_related,
                web_backend=args.web_backend,
                external_plan=external_plan,
                subreddits=subreddits,
                lookback_days=args.lookback_days,
                as_of_date=args.as_of_date,
                plan_queries_only=True,
            )
            from_date, to_date = dates.get_date_range(
                args.lookback_days, as_of_date=args.as_of_date)
            x_cap = config.get("_max_source_fetches") or pipeline.MAX_SOURCE_FETCHES["x"]
            queries = []
            x_count = 0
            for index, sq in enumerate(plan.subqueries, start=1):
                if "x" in sq.sources and x_count < x_cap:
                    x_count += 1
                    queries.append({"id": f"x{x_count}", "source": "x",
                                    "search_query": sq.search_query})
                if "grounding" in sq.sources:
                    queries.append({"id": f"w{index}", "source": "web",
                                    "search_query": sq.search_query})
            payload = {
                "topic": topic,
                "depth": depth,
                "from_date": from_date,
                "to_date": to_date,
                "plan": schema.to_dict(plan),
                "queries": queries,
            }
            rendered_json = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.plan_queries_out:
                Path(args.plan_queries_out).write_text(rendered_json, encoding="utf-8")
            else:
                print(rendered_json)
            return 0
```

Notes for the implementer:
- `json`, `Path`, `dates`, `schema`, `pipeline` are already imported at module top of `last30days.py` — verify and reuse; do not re-import locally except where the file's local style does (`import json as _json` exists in this function; match whichever is cleaner).
- If `schema.to_dict` does not accept a `QueryPlan` directly, use the same serialization drill mode uses at `last30days.py:1185` (`schema.to_dict(plan)`) — it does; copy that call.
- `topic`, `config`, `depth`, `requested_sources`, `x_related`, `subreddits` are all in scope at this point (they are used by `_main_runner` at :2437).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_inject_results.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add skills/last30days/scripts/last30days.py skills/last30days/scripts/lib/pipeline.py tests/test_inject_results.py
git commit -m "feat: add --plan-queries flag emitting the planned X/web queries as JSON"
```

---

### Task 2: Engine `--inject-results`

**Files:**
- Modify: `skills/last30days/scripts/lib/pipeline.py` (new helper above `_retrieve_stream_impl` ~:2891; grounding branch :2918; X branch :3058)
- Modify: `skills/last30days/scripts/last30days.py` (argparse next to `--plan-queries`; load block next to the `--plan` load at :2312-2331)
- Test: `tests/test_inject_results.py` (extend)

**Interfaces:**
- Consumes: the phase-1 payload from Task 1 (`plan` fed back via existing `--plan`; injection keys are the payload's `search_query` strings).
- Produces: CLI `--inject-results FILE` where FILE is:
  ```json
  {"x": {"<search_query>": [{"id": "X1", "text": "...", "url": "https://x.com/u/status/1",
      "author_handle": "u", "date": "2026-07-01",
      "engagement": {"likes": 100, "reposts": 25, "replies": 15, "quotes": 5},
      "why_relevant": "...", "relevance": 0.85}]},
   "web": {"<search_query>": [{"id": "WI1", "title": "...", "url": "https://example.com/a",
      "source_domain": "example.com", "snippet": "...", "date": "2026-07-02",
      "relevance": 0.8, "why_relevant": "hermes web_search"}]}}
  ```
  X items = `xai_x.parse_x_response` output shape; web items = the brave/exa shape (`grounding.py:69-78`). `engagement`/`date` may be null.
- Produces: `pipeline._injected_results(config, kind, query) -> list[dict] | None` (membership-based lookup helper; `kind` is `"x"` or `"web"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inject_results.py`:

```python
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
```

Note: `--mock` short-circuits `_retrieve_stream_impl` before the injection seam (`if mock:` at pipeline.py:2916 returns mock results). Step 3 places the injection checks BEFORE the mock check so injected queries win over mock data, keeping these tests fully offline. Mock data for non-injected sources is unaffected.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_inject_results.py::TestInjectResults -v`
Expected: FAIL — `AttributeError: module 'lib.pipeline' has no attribute '_injected_results'`, then unknown `--inject-results` argument.

- [ ] **Step 3: Implement the injection seam in `pipeline.py`**

1. Add above `_retrieve_stream_impl` (~:2891):

```python
def _injected_results(config: dict[str, Any], kind: str, query: str) -> list[dict] | None:
    """Pre-fetched results injected via --inject-results for (kind, query).

    Returns the injected list on a hit, or None on a miss. Membership-based:
    an empty list is a real zero-result hit, never a fallthrough to live
    backends (injected-only policy — the host that injected results owns the
    X/web credentials; this process must not use its own).
    """
    inj = config.get("_inject_results")
    if not isinstance(inj, dict):
        return None
    bucket = inj.get(kind)
    if isinstance(bucket, dict) and query in bucket:
        items = bucket[query]
        return items if isinstance(items, list) else []
    return None
```

2. In `_retrieve_stream_impl`, BEFORE the `if mock:` check (~:2916), add:

```python
    if source in ("grounding", "x") and config.get("_inject_results") is not None:
        kind = "web" if source == "grounding" else "x"
        injected = _injected_results(config, kind, subquery.search_query)
        if injected is None:
            # Injected-only mode: dynamically generated queries (retry-thin,
            # x-handle supplementals) are outside the inject map — report
            # quiet no-coverage instead of spending this env's credentials.
            print(
                f"[Inject] no injected {kind} results for "
                f"'{subquery.search_query}' — skipping (injected-only mode)",
                file=sys.stderr,
            )
            return [], {}
        if source == "grounding":
            return injected, {
                "label": "injected",
                "webSearchQueries": [subquery.search_query],
                "resultCount": len(injected),
            }
        return injected, {}
```

3. In `last30days.py`, next to the new `--plan-queries-out` argument, add:

```python
    parser.add_argument("--inject-results",
                        help="JSON file of pre-fetched X/web results keyed by source then "
                             "search_query ({\"x\": {q: [items]}, \"web\": {q: [items]}}). "
                             "X/grounding then run injected-only: hits skip live fetch, "
                             "misses are quiet no-coverage. Used with --plan by two-phase "
                             "hosts (e.g. the Hermes plugin).")
```

4. In the research path, directly after the `--plan` parse block (ends ~:2331), add:

```python
        if args.inject_results:
            import json as _json2
            try:
                with open(args.inject_results, encoding="utf-8") as f:
                    config["_inject_results"] = _json2.load(f)
            except (OSError, UnicodeDecodeError, _json2.JSONDecodeError) as exc:
                sys.stderr.write(f"[Inject] Cannot read --inject-results file: {exc}\n")
                raise SystemExit(2)
```

(Match the local import style of the `--plan` block above it; if `json` is already imported in scope as `_json`, reuse it instead of `_json2`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_inject_results.py -v`
Expected: all PASS (Task 1 + Task 2 tests).

- [ ] **Step 5: Run the pre-existing suite for regressions in touched files**

Run: `uv run pytest tests/ -k "pipeline or plan" -q`
Expected: PASS (no regressions from the `run()` signature widening or the new seam).

- [ ] **Step 6: Commit**

```bash
git add skills/last30days/scripts/last30days.py skills/last30days/scripts/lib/pipeline.py tests/test_inject_results.py
git commit -m "feat: add --inject-results for pre-fetched X/web items (injected-only mode)"
```

---

### Task 3: Hermes plugin (`plugin.yaml` + `__init__.py`)

**Files:**
- Create: `plugin.yaml` (repo root)
- Create: `__init__.py` (repo root)
- Test: `tests/test_hermes_plugin_handler.py` (new)

**Interfaces:**
- Consumes: Task 1 CLI (`--plan-queries --plan-queries-out`), Task 2 CLI (`--plan`, `--inject-results`), `lib.xai_x.parse_x_response(response: dict) -> list[dict]`, `lib.xai_x.X_SEARCH_PROMPT` / `DEPTH_CONFIG`.
- Consumes: Hermes `ctx.register_tool(name, toolset, schema, handler)`, `ctx.register_skill(name, path)`, `ctx.dispatch_tool(name, args) -> str` (raw JSON string; `x_search` args: `query`, `from_date`, `to_date`; `web_search` args: `query`, `limit`).
- Produces: tool `last30days_research(topic, depth="default", emit="context", lookback_days=30)` returning a JSON envelope string:
  `{"ok": true, "report": "...", "coverage": {"queries": [{"id", "source", "search_query", "status": "injected|failed|empty", "items": n}], "no_coverage": [...]}, "warnings": [...], "timings": {"plan_s", "fetch_s", "research_s"}, "meta": {...}}`
  or `{"ok": false, "stage": "plan|fetch|research", "error": "...", "stderr_tail": "..."}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hermes_plugin_handler.py`:

```python
"""Tests for the root Hermes plugin handler (fake ctx, canned subprocesses)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_last30days_plugin", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLAN_PAYLOAD = {
    "topic": "test topic", "depth": "default",
    "from_date": "2026-06-20", "to_date": "2026-07-20",
    "plan": {"intent": "news", "subqueries": []},
    "queries": [
        {"id": "x1", "source": "x", "search_query": "test topic"},
        {"id": "w1", "source": "web", "search_query": "test topic news"},
    ],
}

XAI_BLOB = json.dumps({"items": [{
    "text": "post", "url": "https://x.com/u/status/99", "author_handle": "u",
    "date": "2026-07-01", "engagement": None, "why_relevant": "r",
    "relevance": 0.8}]})

WEB_RETURN = json.dumps({"results": [
    {"title": "T", "url": "https://example.com/a", "snippet": "s",
     "publishedDate": "2026-07-02"}]})


class FakeCtx:
    def __init__(self, x_return=XAI_BLOB, web_return=WEB_RETURN, fail=()):
        self.x_return, self.web_return, self.fail = x_return, web_return, fail
        self.dispatched = []
        self.registered_tools = {}
        self.registered_skills = {}

    def register_tool(self, name, toolset=None, schema=None, handler=None, **kw):
        self.registered_tools[name] = handler

    def register_skill(self, name, path):
        self.registered_skills[name] = Path(path)

    def dispatch_tool(self, name, args, **kwargs):
        self.dispatched.append((name, args))
        if name in self.fail:
            raise RuntimeError("backend down")
        return self.x_return if name == "x_search" else self.web_return


def _fake_run_factory(plan_payload=PLAN_PAYLOAD, research_rc=0):
    def fake_run(cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        if "--plan-queries" in cmd:
            out = cmd[cmd.index("--plan-queries-out") + 1]
            Path(out).write_text(json.dumps(plan_payload), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=research_rc,
                               stdout="RENDERED REPORT", stderr="diag")
    return fake_run


class TestRegister:
    def test_registers_tool_and_skill(self):
        plugin = _load_plugin()
        ctx = FakeCtx()
        plugin.register(ctx)
        assert "last30days_research" in ctx.registered_tools
        skill_path = ctx.registered_skills["last30days"]
        assert skill_path.is_absolute()
        assert skill_path == ROOT / "skills" / "last30days" / "SKILL.md"


class TestHandler:
    def _invoke(self, monkeypatch, ctx, fake_run=None, args=None):
        plugin = _load_plugin()
        monkeypatch.setattr(plugin.subprocess, "run",
                            fake_run or _fake_run_factory())
        plugin.register(ctx)
        handler = ctx.registered_tools["last30days_research"]
        return json.loads(handler(args or {"topic": "test topic"}))

    def test_success_envelope(self, monkeypatch):
        ctx = FakeCtx()
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True
        assert result["report"] == "RENDERED REPORT"
        statuses = {q["id"]: q["status"] for q in result["coverage"]["queries"]}
        assert statuses == {"x1": "injected", "w1": "injected"}
        # from/to dates passed as native x_search args, not only prose
        x_calls = [a for n, a in ctx.dispatched if n == "x_search"]
        assert x_calls and x_calls[0]["from_date"] == "2026-06-20"

    def test_dispatch_failure_is_thin_stream_not_fatal(self, monkeypatch):
        ctx = FakeCtx(fail={"x_search"})
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True
        statuses = {q["id"]: q["status"] for q in result["coverage"]["queries"]}
        assert statuses["x1"] == "failed"
        assert result["warnings"]

    def test_research_subprocess_failure_returns_error_envelope(self, monkeypatch):
        ctx = FakeCtx()
        result = self._invoke(
            monkeypatch, ctx, fake_run=_fake_run_factory(research_rc=3))
        assert result["ok"] is False
        assert result["stage"] == "research"

    def test_handler_never_raises_on_malformed_x_return(self, monkeypatch):
        ctx = FakeCtx(x_return="no json here at all")
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True  # x stream empty/failed, run continues


class TestCitationFallback:
    def test_only_status_urls_accepted(self):
        plugin = _load_plugin()
        text = ("see https://x.com/alice/status/111 and "
                "https://twitter.com/bob/status/222?s=20 but not "
                "https://x.com/alice and not https://example.com/status/3 "
                "and dupe https://x.com/alice/status/111")
        items = plugin._items_from_citations(text)
        urls = sorted(i["url"] for i in items)
        assert urls == ["https://twitter.com/bob/status/222",
                        "https://x.com/alice/status/111"]
        assert all(i["engagement"] is None for i in items)
        assert all(i["why_relevant"] == "citation-fallback" for i in items)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hermes_plugin_handler.py -v`
Expected: FAIL — `FileNotFoundError` loading root `__init__.py`.

- [ ] **Step 3: Create `plugin.yaml`**

```yaml
name: last30days
version: "3.16.0"
description: >-
  Research any topic across Reddit, X, YouTube, Hacker News, Polymarket, and
  the web (last 30 days). X and web fetches ride the agent's own x_search /
  web_search tools — no separate credentials.
author: mvanhorn
provides_tools:
  - last30days_research
requires_env: []
```

(If `hermes plugins list` later rejects an unknown field, drop it — `name`, `version`, `description` are the required core. Verify field names against the hermes-agent loader during the VPS release gate.)

- [ ] **Step 4: Create root `__init__.py`**

```python
"""Hermes Agent plugin for last30days.

Registers:
  - tool `last30days_research` — full research run; X and web queries are
    fetched through the agent's own x_search / web_search tools
    (ctx.dispatch_tool), so this plugin needs no credentials of its own.
  - skill `last30days` — the bundled SKILL.md, loadable via
    skill_view("last30days:last30days").

Two-phase inject: engine plans queries (--plan-queries), this handler fetches
them via dispatch_tool, engine re-runs with --plan + --inject-results.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "skills" / "last30days" / "scripts" / "last30days.py"
SKILL_MD = ROOT / "skills" / "last30days" / "SKILL.md"

PLAN_TIMEOUT_S = 180
RESEARCH_TIMEOUT_S = 900
DISPATCH_DEADLINE_S = 300   # total wall clock for all dispatch_tool calls
MAX_WEB_QUERIES = 6
WEB_RESULT_LIMIT = 10
STDERR_TAIL_CHARS = 2000

_STATUS_URL_RE = re.compile(
    r"https?://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d+)")

TOOL_SCHEMA = {
    "name": "last30days_research",
    "description": (
        "Research a topic across Reddit, X, YouTube, Hacker News, Polymarket, "
        "GitHub, and the web over the last 30 days. Returns a ranked, "
        "engagement-weighted report. X and web searches use this agent's own "
        "x_search/web_search tools."),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Topic to research"},
            "depth": {"type": "string", "enum": ["quick", "default", "deep"],
                      "description": "Research depth (default: default)"},
            "emit": {"type": "string",
                     "enum": ["context", "compact", "md", "brief"],
                     "description": "Output format (default: context)"},
            "lookback_days": {"type": "integer",
                              "description": "Days to look back (default 30)"},
        },
        "required": ["topic"],
    },
}


def register(ctx):
    ctx.register_skill("last30days", SKILL_MD)
    ctx.register_tool(
        name="last30days_research",
        toolset="research",
        schema=TOOL_SCHEMA,
        handler=lambda args, **kwargs: _handler(ctx, args),
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _handler(ctx, args: dict) -> str:
    topic = str(args.get("topic") or "").strip()
    if not topic:
        return _error("plan", "missing required argument: topic")
    depth = args.get("depth") or "default"
    emit = args.get("emit") or "context"
    lookback = args.get("lookback_days")

    shared_flags: list[str] = []
    if depth == "quick":
        shared_flags.append("--quick")
    elif depth == "deep":
        shared_flags.append("--deep")
    if lookback:
        shared_flags += ["--days", str(int(lookback))]

    tmpdir = Path(tempfile.mkdtemp(prefix="last30days-hermes-"))
    timings: dict[str, float] = {}
    try:
        # Phase 1: plan
        t0 = time.monotonic()
        plan_out = tmpdir / "plan-queries.json"
        proc = _run_engine(
            [topic, "--plan-queries", "--plan-queries-out", str(plan_out),
             *shared_flags],
            timeout=PLAN_TIMEOUT_S)
        if proc is None or proc.returncode != 0:
            return _error("plan", "engine --plan-queries failed",
                          proc.stderr if proc else "timeout")
        try:
            payload = json.loads(plan_out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _error("plan", f"cannot read plan-queries output: {exc}")
        timings["plan_s"] = round(time.monotonic() - t0, 1)

        # Phase 2: fetch via the agent's own tools
        t0 = time.monotonic()
        inject, coverage, warnings = _fetch_all(ctx, payload, depth)
        timings["fetch_s"] = round(time.monotonic() - t0, 1)

        plan_file = tmpdir / "plan.json"
        _write_private(plan_file, json.dumps(payload["plan"]))
        inject_file = tmpdir / "inject.json"
        _write_private(inject_file, json.dumps(inject))

        # Phase 3: research with injected results
        t0 = time.monotonic()
        proc = _run_engine(
            [topic, "--plan", str(plan_file),
             "--inject-results", str(inject_file),
             "--emit", emit, *shared_flags],
            timeout=RESEARCH_TIMEOUT_S)
        if proc is None or proc.returncode != 0:
            return _error("research", "engine research run failed",
                          proc.stderr if proc else "timeout")
        timings["research_s"] = round(time.monotonic() - t0, 1)

        return json.dumps({
            "ok": True,
            "report": proc.stdout,
            "coverage": coverage,
            "warnings": warnings,
            "timings": timings,
            "meta": {"topic": topic, "depth": depth, "emit": emit,
                     "from_date": payload.get("from_date"),
                     "to_date": payload.get("to_date")},
        })
    except Exception as exc:  # never raise into the registry
        return _error("fetch", f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _fetch_all(ctx, payload: dict, depth: str):
    """Dispatch every planned query through the agent's own tools."""
    inject: dict = {"x": {}, "web": {}}
    per_query: list[dict] = []
    warnings: list[str] = []
    deadline = time.monotonic() + DISPATCH_DEADLINE_S
    web_seen = 0
    for q in payload.get("queries", []):
        qid, source, query = q.get("id"), q.get("source"), q.get("search_query")
        if not query or source not in ("x", "web"):
            continue
        if source == "web":
            web_seen += 1
            if web_seen > MAX_WEB_QUERIES:
                per_query.append({**q, "status": "skipped-budget", "items": 0})
                continue
        if time.monotonic() > deadline:
            per_query.append({**q, "status": "skipped-deadline", "items": 0})
            warnings.append(f"dispatch deadline hit before {qid}")
            continue
        try:
            if source == "x":
                items = _fetch_x(ctx, query, payload, depth)
                inject["x"][query] = items
            else:
                items = _fetch_web(ctx, query, payload)
                inject["web"][query] = items
            per_query.append(
                {**q, "status": "injected" if items else "empty",
                 "items": len(items)})
            if not items:
                # An empty list is still injected: a real zero-result hit.
                per_query[-1]["status"] = "injected"
        except Exception as exc:
            warnings.append(f"{qid} ({source}): {type(exc).__name__}: {exc}")
            per_query.append({**q, "status": "failed", "items": 0})
            # Failed queries stay OUT of the inject map -> engine records
            # quiet no-coverage for them (injected-only mode).
    coverage = {
        "queries": per_query,
        "no_coverage": [q["id"] for q in per_query
                        if q["status"] in ("failed", "skipped-budget",
                                           "skipped-deadline")],
    }
    return inject, coverage, warnings


# ---------------------------------------------------------------------------
# X adapter
# ---------------------------------------------------------------------------

def _fetch_x(ctx, query: str, payload: dict, depth: str) -> list[dict]:
    xai = _engine_lib("xai_x")
    min_items, max_items = xai.DEPTH_CONFIG.get(depth, xai.DEPTH_CONFIG["default"])
    prompt = xai.X_SEARCH_PROMPT.format(
        topic=query,
        from_date=payload.get("from_date", ""),
        to_date=payload.get("to_date", ""),
        min_items=min_items,
        max_items=max_items,
    )
    raw = ctx.dispatch_tool("x_search", {
        "query": prompt,
        "from_date": payload.get("from_date"),
        "to_date": payload.get("to_date"),
    })
    text = _extract_text(raw)
    try:
        # Compat path: hermes hands the prompt to xAI, which usually obeys the
        # embedded {"items": [...]} contract.
        return xai.parse_x_response({"output": text})
    except Exception:
        # Fallback: mine validated x.com/twitter.com status citations only.
        return _items_from_citations(text)


def _items_from_citations(text: str) -> list[dict]:
    """One engagement-null item per unique x.com/twitter.com status URL."""
    items, seen = [], set()
    for match in _STATUS_URL_RE.finditer(text or ""):
        status_id = match.group(1)
        if status_id in seen:
            continue
        seen.add(status_id)
        handle = match.group(0).split("/status/")[0].rsplit("/", 1)[-1]
        items.append({
            "id": f"XC{len(items) + 1}",
            "text": "",
            "url": f"https://x.com/{handle}/status/{status_id}",
            "author_handle": handle,
            "date": None,
            "engagement": None,
            "why_relevant": "citation-fallback",
            "relevance": 0.5,
        })
    return items


# ---------------------------------------------------------------------------
# Web adapter
# ---------------------------------------------------------------------------

def _fetch_web(ctx, query: str, payload: dict) -> list[dict]:
    raw = ctx.dispatch_tool("web_search", {"query": query,
                                           "limit": WEB_RESULT_LIMIT})
    results = _web_results(raw)
    items = []
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            continue
        url = r.get("url") or r.get("link") or ""
        if not url:
            continue
        raw_date = r.get("publishedDate") or r.get("published_date") \
            or r.get("date") or ""
        date = None
        if isinstance(raw_date, str) and raw_date:
            date = raw_date.split("T")[0][:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                date = None  # never infer a date
        items.append({
            "id": f"WI{i + 1}",
            "title": r.get("title", ""),
            "url": url,
            "source_domain": _domain(url),
            "snippet": (r.get("snippet") or r.get("text")
                        or r.get("description") or "")[:500],
            "date": date,
            "relevance": 0.8,
            "why_relevant": "hermes web_search",
        })
    return items


def _web_results(raw) -> list:
    """Hermes returns the handler's JSON string; accept the common shapes."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine_lib(name: str):
    scripts = str(ROOT / "skills" / "last30days" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import importlib
    return importlib.import_module(f"lib.{name}")


def _extract_text(raw) -> str:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    else:
        data = raw
    if isinstance(data, dict):
        for key in ("output", "text", "content", "result", "answer"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return raw if isinstance(raw, str) else json.dumps(data)


def _run_engine(argv: list[str], *, timeout: int):
    """Run the engine; subprocess.run kills and reaps the child on timeout."""
    try:
        return subprocess.run(
            [sys.executable, str(ENGINE), *argv],
            capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        return None


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)


def _domain(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc.removeprefix("www.")


def _redact(text: str) -> str:
    """Mask any env secret values that leak into diagnostics."""
    if not text:
        return text
    for key, val in os.environ.items():
        if val and len(val) >= 8 and any(
                marker in key for marker in
                ("KEY", "TOKEN", "SECRET", "PASSWORD", "CT0")):
            text = text.replace(val, f"<{key}>")
    return text


def _error(stage: str, message: str, stderr: str = "") -> str:
    return json.dumps({
        "ok": False,
        "stage": stage,
        "error": message,
        "stderr_tail": _redact((stderr or "")[-STDERR_TAIL_CHARS:]),
    })
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_hermes_plugin_handler.py -v`
Expected: all PASS. If `test_success_envelope` fails on coverage statuses, check `_fetch_all`'s status bookkeeping (an injected empty list must report `"injected"`).

- [ ] **Step 6: Verify the whole suite still collects with a root `__init__.py`**

Run: `uv run pytest --collect-only -q | tail -3`
Expected: same test count as before plus the new files; no import errors. (A root `__init__.py` can change pytest's rootdir package semantics — if collection breaks, add `norecursedirs`/`rootdir` config to `pyproject.toml` `[tool.pytest.ini_options]` rather than renaming the file; Hermes requires `__init__.py` at the plugin dir root.)

- [ ] **Step 7: Commit**

```bash
git add plugin.yaml __init__.py tests/test_hermes_plugin_handler.py
git commit -m "feat: add Hermes Agent plugin (last30days_research tool + bundled skill)"
```

---

### Task 4: Version lockstep for `plugin.yaml`

**Files:**
- Modify: `tests/test_plugin_contract.py` (append to `TestPluginContract`, after `test_versions_match_across_manifests` at :70)

**Interfaces:**
- Consumes: `pyproject.toml [project].version` (already parsed in `test_versions_match_across_manifests` via `tomllib`).
- Produces: CI guarantee that `plugin.yaml` version/name never drift.

- [ ] **Step 1: Write the failing test**

Append to `TestPluginContract` in `tests/test_plugin_contract.py`:

```python
    def test_hermes_plugin_yaml_version_matches(self) -> None:
        # No yaml dependency in this repo: parse the two fields with regexes.
        import re
        import tomllib

        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        name = re.search(r'^name:\s*"?([\w-]+)"?\s*$', text, re.MULTILINE)
        version = re.search(r'^version:\s*"?([\d.]+)"?\s*$', text, re.MULTILINE)
        self.assertIsNotNone(name, "plugin.yaml missing name")
        self.assertIsNotNone(version, "plugin.yaml missing version")
        self.assertEqual("last30days", name.group(1))

        pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["version"], version.group(1))
```

(If `tomllib`/`re` are already imported at module top — `tomllib` is — use the module-level imports instead of local ones.)

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_plugin_contract.py -v`
Expected: PASS immediately (Task 3 created `plugin.yaml` at 3.16.0). To prove the test bites, temporarily change `version:` in `plugin.yaml` to `0.0.0`, re-run (expect FAIL), revert.

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_contract.py
git commit -m "test: lock hermes plugin.yaml into the manifest version lockstep"
```

---

### Task 5: Documentation

**Files:**
- Modify: `HERMES_SETUP.md` (new top section)
- Modify: `CONFIGURATION.md` (per-run flags section — the doc is organized per-run flags → env vars → trend stack → per-client patterns; add to the flags section and the Hermes per-client section)
- Modify: `skills/last30days/SKILL.md` (flags reference only — do NOT touch Step 0 onboarding)
- Modify: `CHANGELOG.md` (new entry at top, matching existing entry format)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-3.

- [ ] **Step 1: HERMES_SETUP.md — add a "Plugin install (recommended)" section above the existing skill-install instructions**

Content to convey (write in the file's existing voice/format):

```markdown
## Plugin install (recommended)

    hermes plugins install vcolombo/last30days-hermes-plugin
    hermes plugins enable last30days

Registers two things:

- **Tool `last30days_research`** — full research run. X and web queries are
  fetched through the agent's own `x_search` / `web_search` tools, so **no
  separate X/web credentials are needed**. All other sources behave exactly
  as in the skill install (keyless Reddit/HN/Polymarket work out of the box;
  ScrapeCreators-backed sources still use `SCRAPECREATORS_API_KEY` from the
  agent environment).
- **Bundled skill** — load with `skill_view("last30days:last30days")`.
  Plugin skills are explicit-load only (not in the system-prompt skill index).

Requirements: `x_search` enabled (`hermes tools`; needs xAI OAuth or
`XAI_API_KEY` on the agent) and web search configured. The engine subprocess
runs on the Hermes runtime Python via `sys.executable` — no `python3.12`
binary needed.

**Dual-install note:** installing the plugin does not remove or refresh a
previously installed flat skill (`~/.hermes/skills/.../last30days`). If both
are present, the plugin's bundled skill and tool are authoritative; uninstall
the flat skill or keep it deliberately pinned. Versions can drift otherwise.

To update: `hermes plugins update last30days`.
```

- [ ] **Step 2: CONFIGURATION.md — document the three new flags**

Add to the per-run flags section:

```markdown
- `--plan-queries` / `--plan-queries-out <file>` — plan-only mode: write the
  X/web queries the run would execute (plus the serialized plan) as JSON and
  exit without fetching. For two-phase hosts that fetch X/web themselves
  (e.g. the Hermes plugin). Single-entity topics only.
- `--inject-results <file>` — JSON of pre-fetched X/web results keyed by
  source then search_query (`{"x": {q: [items]}, "web": {q: [items]}}`).
  X/web then run injected-only: hits skip live fetch, misses are quiet
  no-coverage (this process never spends its own X/web credentials). Use
  together with `--plan` from the same `--plan-queries` output.
```

And in the Hermes per-client section, mention the plugin install path (one line + pointer to HERMES_SETUP.md).

- [ ] **Step 3: SKILL.md — add the flags to the flags reference**

In the flags/CLI reference section of `skills/last30days/SKILL.md` (NOT Step 0), add the same two flag entries as CONFIGURATION.md, plus one sentence: "These exist for two-phase hosts (e.g. the Hermes plugin) where the harness fetches X/web through its own tools and injects the results."

- [ ] **Step 4: CHANGELOG.md — add entry**

Match the existing entry format; content:

```markdown
## 3.17.0 — Hermes Agent plugin

- New: native Hermes plugin (`plugin.yaml` + root `__init__.py`) — install
  with `hermes plugins install vcolombo/last30days-hermes-plugin`. Registers
  the `last30days_research` tool (X/web ride the agent's own
  `x_search`/`web_search` credentials) and the bundled skill.
- New engine flags: `--plan-queries` / `--plan-queries-out` (plan-only JSON)
  and `--inject-results` (pre-fetched X/web items, injected-only mode).
```

**Version decision:** this plan does NOT bump `pyproject.toml` — if the maintainer wants 3.17.0, bump `pyproject.toml [project].version`, SKILL.md frontmatter, all four plugin manifests, `gemini-extension.json`, AND `plugin.yaml` together (the lockstep test enforces it). Otherwise title the changelog entry `Unreleased`.

- [ ] **Step 5: Run the doc-adjacent tests**

Run: `uv run pytest tests/test_plugin_contract.py tests/test_onboarding_contract.py -q`
Expected: PASS (SKILL.md Step 0 untouched, manifests still in lockstep).

- [ ] **Step 6: Commit**

```bash
git add HERMES_SETUP.md CONFIGURATION.md skills/last30days/SKILL.md CHANGELOG.md
git commit -m "docs: document Hermes plugin install and two-phase inject flags"
```

---

### Task 6: Full verification

- [ ] **Step 1: Full test suite**

Run: `uv run pytest`
Expected: all green, coverage floor (`fail_under`) holds.

- [ ] **Step 2: Manual CLI round-trip (offline)**

```bash
python3 skills/last30days/scripts/last30days.py "test topic" --plan-queries \
  --plan-queries-out /tmp/claude-plan.json --mock
python3 - <<'EOF'
import json
p = json.load(open("/tmp/claude-plan.json"))
inject = {"x": {}, "web": {}}
for q in p["queries"]:
    b = "x" if q["source"] == "x" else "web"
    inject[b][q["search_query"]] = []
json.dump(inject, open("/tmp/claude-inject.json", "w"))
json.dump(p["plan"], open("/tmp/claude-planonly.json", "w"))
EOF
python3 skills/last30days/scripts/last30days.py "test topic" \
  --plan /tmp/claude-planonly.json --inject-results /tmp/claude-inject.json \
  --mock --emit compact
```

Expected: both runs exit 0; second run's stderr shows `[Inject]`-labeled hits/misses and NO live X/web fetch attempts.

- [ ] **Step 3: Release gates on the VPS (Benson's box) — requires network/docker access**

1. Capture real dispatch returns as fixtures: ask Benson (per `talking-to-benson` skill) to run `x_search` and `web_search` once each and paste raw returns; save under `tests/fixtures/hermes/`; eyeball `_extract_text`/`_web_results` against them (adjust adapters if shapes differ).
2. Engine self-containment: `docker exec -u 10000:10000 hermes-agent sh -c '/opt/hermes/.venv/bin/python /opt/data/plugins/last30days/skills/last30days/scripts/last30days.py --diagnose'` (after install).
3. Install + live run: `hermes plugins install vcolombo/last30days-hermes-plugin`, `hermes plugins enable last30days`, `HERMES_PLUGINS_DEBUG=1 hermes plugins list` (clean discovery), then a live `last30days_research` call via `hermes chat` — confirm report + coverage envelope.
4. Post-install review by Benson; dual-review bar (Benson + codex) before calling it production-ready.

- [ ] **Step 4: Push branch / open PR per repo workflow**

---

## Self-review notes

- Spec coverage: plugin.yaml/`__init__.py` (Task 3), engine flags (Tasks 1-2), injected-only + membership semantics (Task 2), citation-fallback validation + native date args + budgets + envelope + 0600 + redaction (Task 3), lockstep test (Task 4), docs incl. dual-install drift (Task 5), release gates (Task 6). Out-of-scope items from the spec are not implemented anywhere (correct).
- Type consistency: `_injected_results(config, kind, query)` used identically in tests and pipeline; handler name `last30days_research` consistent across plugin.yaml, schema, register, tests.
- Known judgment calls for the implementer: exact placement line numbers may drift a few lines — anchor on the named code landmarks, not absolute numbers; hermes `plugin.yaml` field names and `register_tool` kwargs verified against the live loader at the VPS gate.
