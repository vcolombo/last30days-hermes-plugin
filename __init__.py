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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "skills" / "last30days" / "scripts" / "last30days.py"
SKILL_MD = ROOT / "skills" / "last30days" / "SKILL.md"

PLAN_TIMEOUT_S = 180
RESEARCH_TIMEOUT_S = 900
DISPATCH_DEADLINE_S = 300   # total wall clock for all dispatch_tool calls
DISPATCH_CALL_TIMEOUT_S = 120  # per-dispatch bound; a hung tool must not block the worker
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

def _handler(ctx, args) -> str:
    tmpdir: Path | None = None
    timings: dict[str, float] = {}
    try:
        if not isinstance(args, dict):
            return _error("plan", "arguments must be an object")
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
            try:
                shared_flags += ["--days", str(int(lookback))]
            except (TypeError, ValueError):
                return _error("plan", f"invalid lookback_days: {lookback!r}")

        tmpdir = Path(tempfile.mkdtemp(prefix="last30days-hermes-"))
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
            "warnings": [_redact(w) for w in warnings],
            "timings": timings,
            "meta": {"topic": topic, "depth": depth, "emit": emit,
                     "from_date": payload.get("from_date"),
                     "to_date": payload.get("to_date")},
        })
    except Exception as exc:  # never raise into the registry
        return _error("fetch", f"{type(exc).__name__}: {exc}")
    finally:
        if tmpdir is not None:
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
            # An empty list is still injected: a real zero-result hit.
            per_query.append(
                {**q, "status": "injected", "items": len(items)})
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


def _dispatch(ctx, name: str, args: dict, timeout_s: float | None = None) -> str:
    """dispatch_tool with a hard timeout.

    ctx.dispatch_tool is synchronous; a stalled backend would otherwise hang
    the Hermes worker past every budget. On timeout the worker thread may
    linger until the call returns, but this handler regains control and
    reports the query as failed.
    """
    if timeout_s is None:
        timeout_s = DISPATCH_CALL_TIMEOUT_S
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(ctx.dispatch_tool, name, args)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            raise RuntimeError(
                f"{name} dispatch timed out after {timeout_s:.0f}s")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


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
    raw = _dispatch(ctx, "x_search", {
        "query": prompt,
        "from_date": payload.get("from_date"),
        "to_date": payload.get("to_date"),
    })
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(_unwrap(raw))
        except json.JSONDecodeError:
            data = None  # plain text; fall through to the text path
    if isinstance(data, dict):
        _raise_on_tool_error(data)
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
            "url": match.group(0),
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
    raw = _dispatch(ctx, "web_search", {"query": query,
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
            data = json.loads(_unwrap(raw))
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        _raise_on_tool_error(data)
        # Real Hermes v0.18.2 shape: {"success": true, "data": {"web": [...]}}
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("web", "results", "items"):
                if isinstance(inner.get(key), list):
                    return inner[key]
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


def _unwrap(raw: str) -> str:
    """Strip Hermes' <untrusted_tool_result> wrapper + prose banner, if any."""
    open_at = raw.find("<untrusted_tool_result")
    if open_at == -1:
        return raw
    start = raw.find(">", open_at) + 1
    end = raw.find("</untrusted_tool_result>", start)
    inner = raw[start:end if end != -1 else len(raw)]
    brace = inner.find("{")  # adapters only ever need the JSON object
    return inner[brace:] if brace != -1 else inner


def _raise_on_tool_error(data: dict) -> None:
    """Explicit tool failure -> raise so _fetch_all marks the query failed."""
    payload_keys = ("data", "results", "items", "web",
                    "output", "text", "content", "result", "answer")
    if data.get("success") is False or (
            data.get("error") and not any(data.get(k) for k in payload_keys)):
        raise RuntimeError(
            f"{data.get('tool') or 'tool'} error: {data.get('error')}")


def _extract_text(raw) -> str:
    if isinstance(raw, str):
        raw = _unwrap(raw)
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
