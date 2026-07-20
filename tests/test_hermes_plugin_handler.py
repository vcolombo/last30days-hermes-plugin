"""Tests for the root Hermes plugin handler (fake ctx, canned subprocesses)."""

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "hermes"
X_ERROR_FIXTURE = (FIXTURES / "x_search_error.json").read_text(encoding="utf-8")
WEB_WRAPPED_FIXTURE = (FIXTURES / "web_search_wrapped.txt").read_text(
    encoding="utf-8")


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

    def test_success_envelope_warnings_are_redacted(self, monkeypatch):
        """Env secrets leaking into dispatch-failure messages must be masked
        in the success envelope's warnings, same as error stderr tails."""
        monkeypatch.setenv("FAKE_API_KEY", "secret12345")

        class LeakyCtx(FakeCtx):
            def dispatch_tool(self, name, args, **kwargs):
                if name == "x_search":
                    raise RuntimeError("auth failed with secret12345")
                return super().dispatch_tool(name, args, **kwargs)

        result = self._invoke(monkeypatch, LeakyCtx())
        assert result["ok"] is True
        assert result["warnings"]
        joined = " ".join(result["warnings"])
        assert "secret12345" not in joined
        assert "<FAKE_API_KEY>" in joined

    def test_research_subprocess_failure_returns_error_envelope(self, monkeypatch):
        ctx = FakeCtx()
        result = self._invoke(
            monkeypatch, ctx, fake_run=_fake_run_factory(research_rc=3))
        assert result["ok"] is False
        assert result["stage"] == "research"

    def test_bad_lookback_days_returns_error_envelope_not_raise(self, monkeypatch):
        ctx = FakeCtx()
        result = self._invoke(
            monkeypatch, ctx,
            args={"topic": "test topic", "lookback_days": "abc"})
        assert result["ok"] is False
        assert result["stage"] == "plan"

    def test_handler_never_raises_on_malformed_x_return(self, monkeypatch):
        ctx = FakeCtx(x_return="no json here at all")
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True  # x stream empty/failed, run continues

    def test_x_search_error_return_marks_query_failed(self, monkeypatch):
        """Real Hermes v0.18.2 x_search failure return -> failed, not injected."""
        ctx = FakeCtx(x_return=X_ERROR_FIXTURE)
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True
        statuses = {q["id"]: q["status"] for q in result["coverage"]["queries"]}
        assert statuses["x1"] == "failed"
        assert "x1" in result["coverage"]["no_coverage"]
        assert any("spending-limit" in w for w in result["warnings"])

    def test_web_results_parses_wrapped_data_web_shape(self, monkeypatch):
        """Real wrapped web_search return: banner stripped, data.web found."""
        plugin = _load_plugin()
        results = plugin._web_results(WEB_WRAPPED_FIXTURE)
        assert len(results) == 2
        assert all(isinstance(r, dict) for r in results)
        assert "https://hermesatlas.com/" in [r["url"] for r in results]
        # End to end: query injected with 2 items, snippet from description.
        ctx = FakeCtx(web_return=WEB_WRAPPED_FIXTURE)
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True
        by_id = {q["id"]: q for q in result["coverage"]["queries"]}
        assert by_id["w1"]["status"] == "injected"
        assert by_id["w1"]["items"] == 2
        items = plugin._fetch_web(FakeCtx(web_return=WEB_WRAPPED_FIXTURE),
                                  "q", {})
        assert items[0]["snippet"] == "Hermes Agent, memory, and the ecosystem..."

    def test_hung_dispatch_times_out_and_marks_query_failed(self, monkeypatch):
        """A stalled dispatch_tool must not hang the worker: _dispatch bounds
        each call, the query is marked failed, and the run continues."""
        class SlowCtx(FakeCtx):
            def dispatch_tool(self, name, args, **kwargs):
                if name == "x_search":
                    time.sleep(3)
                return super().dispatch_tool(name, args, **kwargs)

        plugin = _load_plugin()
        monkeypatch.setattr(plugin, "DISPATCH_CALL_TIMEOUT_S", 0.2)
        monkeypatch.setattr(plugin.subprocess, "run", _fake_run_factory())
        ctx = SlowCtx()
        plugin.register(ctx)
        handler = ctx.registered_tools["last30days_research"]
        t0 = time.monotonic()
        result = json.loads(handler({"topic": "test topic"}))
        assert time.monotonic() - t0 < 3  # regained control before the sleep
        assert result["ok"] is True
        statuses = {q["id"]: q["status"] for q in result["coverage"]["queries"]}
        assert statuses["x1"] == "failed"
        assert any("timed out" in w for w in result["warnings"])

    def test_non_dict_args_return_error_envelope_not_raise(self, monkeypatch):
        """None/list args must yield a JSON ok:false envelope, never an
        exception into the registry."""
        plugin = _load_plugin()
        ctx = FakeCtx()
        plugin.register(ctx)
        handler = ctx.registered_tools["last30days_research"]
        for bad in (None, [1]):
            result = json.loads(handler(bad))
            assert result["ok"] is False
            assert "arguments" in result["error"]

    def test_web_search_error_marks_query_failed(self, monkeypatch):
        ctx = FakeCtx(web_return=json.dumps(
            {"success": False, "error": "quota"}))
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True
        statuses = {q["id"]: q["status"] for q in result["coverage"]["queries"]}
        assert statuses["w1"] == "failed"


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


X_SUCCESS_ITEMS_FIXTURE = (
    FIXTURES / "x_search_success_items.json").read_text(encoding="utf-8")
X_SUCCESS_PLAIN_FIXTURE = (
    FIXTURES / "x_search_success_plain.json").read_text(encoding="utf-8")


class TestLiveSuccessFixtures:
    """Real Hermes v0.18.2 x_search success returns (captured live 2026-07-20)."""

    def _invoke(self, monkeypatch, ctx):
        plugin = _load_plugin()
        monkeypatch.setattr(plugin.subprocess, "run", _fake_run_factory())
        plugin.register(ctx)
        handler = ctx.registered_tools["last30days_research"]
        return json.loads(handler({"topic": "test topic"}))

    def test_structured_prompt_return_parses_full_items(self, monkeypatch):
        # With the engine's X_SEARCH_PROMPT, grok returns the {"items": [...]}
        # blob in "answer" — the primary parse path, real engagement included.
        ctx = FakeCtx(x_return=X_SUCCESS_ITEMS_FIXTURE)
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True
        x1 = next(q for q in result["coverage"]["queries"] if q["id"] == "x1")
        assert x1["status"] == "injected"
        assert x1["items"] == 10

    def test_plain_answer_return_falls_back_to_citations(self, monkeypatch):
        # A narrative (non-blob) answer degrades to the citation fallback:
        # unique x.com status URLs mined from the markdown, engagement null.
        ctx = FakeCtx(x_return=X_SUCCESS_PLAIN_FIXTURE)
        result = self._invoke(monkeypatch, ctx)
        assert result["ok"] is True
        x1 = next(q for q in result["coverage"]["queries"] if q["id"] == "x1")
        assert x1["status"] == "injected"
        assert x1["items"] == 7
