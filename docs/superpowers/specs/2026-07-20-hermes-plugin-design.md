# Design: last30days as a Hermes Agent Plugin

**Date:** 2026-07-20
**Status:** Approved (Vincent + Benson review, session `20260720_144803_31718e`)

## Problem

Running /last30days under Hermes requires separate credentials: X needs `AUTH_TOKEN`/`CT0` session cookies (vendored bird-search) or `XAI_API_KEY`, web search needs Brave/Exa/Serper keys — even though the Hermes agent already has credentialed `x_search` and `web_search` tools. Users configure the same capability twice.

## Goal

Package this fork as a native Hermes plugin whose research tool routes X and web fetches through the agent's own tools (`ctx.dispatch_tool`), feeding results into the engine's existing normalization/ranking pipeline. Zero plugin-level credentials (`requires_env: []`). Sources Hermes has no native tool for (Reddit, HN, Polymarket — keyless; ScrapeCreators-backed sources) keep fetching in-engine as today.

## Verified platform facts (Hermes v0.18.2, Benson's install)

- `x_search` enabled (xAI Responses backend; SuperGrok OAuth or `XAI_API_KEY`). Args: `query` (required), `from_date`, `to_date`, `allowed_x_handles`, `excluded_x_handles`, image/video understanding flags.
- `web_search` enabled. Args: `query` (required), `limit` (1-100, default 5). Backend is opaque (may be Nous-managed) — never branch on presumed env keys.
- `ctx.dispatch_tool(name, args)` = direct registry dispatch; returns the handler's raw JSON string; catches handler exceptions; does NOT inherit session/platform tool allowlists; searches raise no approval prompts.
- Runtime Python 3.13.5; `python3.12` binary absent — subprocesses must use `sys.executable`.
- Plugins live at `/opt/data/plugins/<name>/`; `hermes plugins install` clones the repo but installs no pip deps (engine is self-contained; smoke-tested as a release gate).
- Plugin-bundled skills are explicit-load only (`skill_view("last30days:last30days")`), not in the system-prompt skill index; namespace derives from `plugin.yaml:name`.

## Architecture

Plugin at repo root (`plugin.yaml` + `__init__.py`), installed via `hermes plugins install vcolombo/last30days-hermes-plugin`. Registers:

1. **Tool** `last30days_research(topic, depth=default, emit=context, lookback_days=30)`
2. **Bundled skill** — existing `skills/last30days/SKILL.md` via `ctx.register_skill` (absolute path from `__file__`; gateway cwd unstable)

### Two-phase inject flow (tool handler)

1. **Plan** — subprocess `last30days.py <topic> --plan-queries --plan-queries-out <file>` (new engine flags). Engine plans queries and writes JSON to the file (stdout purity not guaranteed): `{plan: schema.to_dict(plan), queries: [{id, source: "x"|"web", search_query}], from_date, to_date}`. Stable query IDs assigned per (source, subquery). X queries capped by existing `MAX_SOURCE_FETCHES`.
2. **Fetch** — serial `ctx.dispatch_tool` loop (Hermes handler thread-safety unverified). X: pass `from_date`/`to_date` as native args; prefer Hermes structured return fields; `xai_x.parse_x_response` as compat path for the `{"items":[...]}` blob; citation fallback ONLY for validated `x.com`/`twitter.com` status URLs (status-ID extraction, canonical dedupe, explicit provenance). Web: adapt to grounding item shape (`title/url/snippet/date`; date stays null when absent — never inferred). Per-query try/except → thin stream + warning. Budgets: per-dispatch timeout, capped query counts, total wall-clock deadline.
3. **Research** — subprocess `last30days.py <topic> --plan <planfile> --inject-results <injectfile> --emit <emit>` with the identical shared flag list (fetch-cap math must match phase 1). Engine consumes injected results; all other sources fetch normally.

Handler contract: never raises (but does not swallow cancellation/termination); kills and reaps timed-out subprocesses; temp files 0600 in a private tempdir, removed in `finally`; stderr tails capped and secret-redacted.

**Result envelope** (JSON string): `{ok, report, coverage: {per-query outcomes, injected counts, no-coverage queries}, warnings, timings, meta}`; errors `{ok: false, stage, error, stderr_tail}`. Fatal failure, partial coverage, and success-with-zero-results are distinguishable.

### Engine changes (two flags)

- `--plan-queries` / `--plan-queries-out`: `pipeline.run(plan_queries_only=True)` forces `x`+`grounding` into available sources (planner assigns them even keyless) and returns the plan immediately before fetch starts (`pipeline.py:1412` seam). Existing `--plan` round-trip (`planner._sanitize_plan`) carries the plan to phase 3. v1: single-entity plans only.
- `--inject-results <file>`: source-qualified map keyed by stable query ID. X (`pipeline.py:3058`) and grounding (`:2918`) branches check the map first — **membership-based**, so `[]` is a hit with zero results. **Injected-only policy**: with injection active, X/grounding lookup misses (retry-thin, `--x-handle` supplementals) return empty with a recorded no-coverage note — never fall through to live credentialed backends. Injected items ride the existing `normalize.normalize_source_items` path; zero new normalizers (xai item shape for X, grounding shape for web; null engagement/date already handled).

### Quality note

Injected X = xAI-method parity (engagement often model-reported or null), weaker than cookie-based bird method. Accepted: identical degradation to the engine's existing `xai_x` path, not worse.

## Error handling

- Failed dispatch for one query → that stream is thin, run continues, warning in envelope.
- Malformed x_search return → structured-field adapter → parse_x_response compat → validated-citation fallback → skip item.
- Subprocess non-zero/timeout → `ok: false` envelope with stage + redacted stderr tail.
- Empty injection (`[]`) → legitimate zero-result hit, no fallback fetch.

## Testing

- `tests/test_plugin_contract.py`: plugin.yaml joins version lockstep with `pyproject.toml` (regex parse, no yaml dep).
- `tests/test_inject_results.py`: plan-queries JSON sanity + `_sanitize_plan` round-trip; inject round-trip through `pipeline.run` with network monkeypatched off (injected URLs ranked, engagement scored, empty-list = hit, miss = quiet no-coverage).
- `tests/test_hermes_plugin_handler.py`: fake `ctx`, monkeypatched `subprocess.run`; envelope shape, no-raise, tempdir cleanup, citation-fallback URL validation.
- **Release gates (VPS)**: live `dispatch_tool` return fixtures on the target Hermes build; engine self-containment via `sys.executable ... --diagnose` in the container; real `hermes plugins install` + live run.

## Recorded disagreements with Benson

- His concern #1 (inject map source-collision) partly misread the design — the map was already source-qualified; only the stable-ID recommendation was folded.
- His concern #3 (scrub subprocess env): not folded wholesale — scrubbing would break sources Hermes doesn't cover (ScrapeCreators etc.). The injected-only policy for X/grounding addresses the actual credential-leak risk.

## Out of scope (v1)

Competitors/vs-mode through the tool; `--inject-fallback=live` hybrid mode; parallel dispatch; routing dynamically generated queries (retry-thin/handle supplementals) through Hermes — they surface as reported no-coverage.
