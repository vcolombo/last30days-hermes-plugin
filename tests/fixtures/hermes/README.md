# Hermes tool-return fixtures

Captured 2026-07-20 from Hermes v0.18.2 via live `x_search` / `web_search` calls; stored verbatim.

- `x_search_error.json` — spending-limit failure shape
- `web_search_wrapped.txt` — web_search success wrapped in `<untrusted_tool_result>`
- `x_search_success_plain.json` — plain-query success: narrative `answer` + `inline_citations` (citation-fallback path)
- `x_search_success_items.json` — engine `X_SEARCH_PROMPT` success: `answer` carries the `{"items": [...]}` blob with real engagement (primary parse path)
