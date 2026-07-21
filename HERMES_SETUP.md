# Hermes Setup Guide for last30days

This guide covers installing last30days on Hermes AI Agent.

## Prerequisites

1. **Hermes installed** - See https://github.com/NousResearch/hermes-agent
2. **Python 3.12+** - `brew install python@3.12` or similar
3. **yt-dlp** (optional, for YouTube) - `brew install yt-dlp`

## Plugin install (recommended)

```bash
hermes plugins install vcolombo/last30days-hermes-plugin
hermes plugins enable last30days
```

This registers two things:

- **Tool `last30days_research`** — a full research run as a native Hermes tool. X and web queries are fetched through the agent's own `x_search` / `web_search` tools, so **no separate X/web credentials are needed**. Every other source behaves exactly as in the skill install below: keyless Reddit/HN/Polymarket work out of the box, and ScrapeCreators-backed sources still read `SCRAPECREATORS_API_KEY` from the agent environment.
- **Bundled skill** — load it with `skill_view("last30days:last30days")`. Plugin skills are explicit-load only; they do not appear in the system-prompt skill index.

**Requirements:** `x_search` enabled (check with `hermes tools`; it needs xAI OAuth or `XAI_API_KEY` on the agent) and web search configured. The engine subprocess runs on the Hermes runtime Python via `sys.executable`, so no separate `python3.12` binary is needed for the plugin path.

**Dual-install note:** installing the plugin does not remove or refresh a previously installed flat skill (`~/.hermes/skills/.../last30days`). If both are present, the plugin's bundled skill and tool are authoritative — uninstall the flat skill, or keep it deliberately pinned. Versions can drift otherwise.

To update: `hermes plugins update last30days`.

## Skill install (alternative)

```bash
hermes skills install mvanhorn/last30days-skill/skills/last30days --force
```

The explicit `skills/last30days` path fetches the skill straight from this repo's current default branch and deploys it under `~/.hermes/skills/`. `--force` is required because Hermes's install-time security scanner returns a `caution` verdict for this skill — it flags benign patterns such as reading your own API keys from the environment and calling `subprocess` to run `yt-dlp`/`bird`. `--force` accepts the caution verdict and installs (it also reinstalls over any existing copy).

**Why the explicit path?** The shorter `hermes skills install mvanhorn/last30days-skill` currently resolves through the skills.sh index, which is serving an older cached snapshot of this repo (from before the skill moved under `skills/last30days/`). Use the explicit `.../skills/last30days` path above until the index re-crawls — tracked in [vercel-labs/skills#1602](https://github.com/vercel-labs/skills/issues/1602).

### Developer / live-edit alternative

If you're hacking on the skill locally and want edits to propagate to Hermes without re-installing, symlink your working tree:

```bash
git clone https://github.com/mvanhorn/last30days-skill.git
mkdir -p ~/.hermes/skills/research
ln -s "$(pwd)/last30days-skill/skills/last30days" ~/.hermes/skills/research/last30days
```

## Usage

In Hermes, invoke with:

```
last30days "your research topic"
```

Or with options:
```
last30days "best mechanical keyboards 2025" --search=reddit,youtube
last30days "AI news" --days=7 --deep
```

## First Run Setup

On first run, the skill will guide you through setup:

1. **Auto setup** (~30 seconds)
   - Scans browser cookies for X/Twitter
   - Checks/installs yt-dlp for YouTube
   - Best-effort install of `digg-pp-cli` for Digg AI-news clusters (via `@mvanhorn/printing-press-library`; binary lands in `$HOME/.local/bin` — ensure your Hermes gateway PATH includes it, or Digg stays off even after install)
   - Configures free sources (Reddit, HN, Polymarket)

2. **Optional: ScrapeCreators**
   - Adds TikTok, Instagram, Reddit backup
   - 100 free credits (no expiration)
   - Sign up at scrapecreators.com

3. **Optional: API Keys**
   - XAI_API_KEY for X/Twitter (alternative to browser cookies)
   - BRAVE_API_KEY for web search

## Available Sources

### Free (No API Key)
- **Reddit** - Public discussions and comments
- **Hacker News** - Tech discussions via Algolia
- **Polymarket** - Prediction markets
- **YouTube** - Search and transcripts (requires yt-dlp)
- **Digg** - AI-news story clusters (requires `digg-pp-cli` on the agent PATH; auto-installed to `$HOME/.local/bin` during setup when `npx` is available)

### Requires API Key
- **X/Twitter** - xAI API key or browser cookies
- **TikTok** - ScrapeCreators API
- **Instagram** - ScrapeCreators API
- **Web Search** - Brave Search API

## Troubleshooting

### Python not found
```bash
# Find Python 3.12+
which python3.12 python3.13 python3.14

# If not installed
brew install python@3.12
```

### yt-dlp not found
```bash
brew install yt-dlp
# or
pip install yt-dlp
```

### Check what's configured
```bash
cd ~/.hermes/skills/research/last30days
python3.12 scripts/last30days.py --diagnose
```

## Recurring monitoring (Hermes cron)

Schedule agent-native trend-monitoring: a cron turn calls the plugin (so X/web
ride your own `x_search`/`web_search` — no separate credentials) and reports
only what's NEW since the last delivered run.

**Delivery guarantee (at-least-once, carry-forward).** The delta unions every
monitor run since the last *acked* run, so a finding from a run whose delivery
failed is carried forward until delivered. Precisely: a **later successful
recurring monitor run** re-includes any completed, persisted, un-acked finding.
This is *not* "Hermes retries a failed turn" — an interrupted cron attempt is
recorded `unknown` and is not rerun. Delivery is therefore eventual only while
the job keeps running and eventually succeeds: a disabled/paused job, a
persistently failing provider, or a run killed *before* it persists is outside
the guarantee. Concurrent runs of the same monitor+topic serialize on a DB lease;
watermarks are per `(monitor, topic)`; acks are validated and monotonic — so the
guarantee holds across concurrent, multi-process, and manual-vs-scheduled
collisions, not only a serial cron. The one residual is a **re-send**: a crash
after `hermes send` succeeds but before the ack makes the next run re-report the
same items (at-least-once, not exactly-once).

**Bounded exception (possible loss).** The never-lose property covers runs that
complete *under their lease*. If a run holds the lease and the process is then
paused past the lease TTL (`LEASE_TTL_SECONDS`, 1800s) — container/VM suspension,
filesystem freeze, scheduler starvation, or a cron inactivity-timeout that
interrupts the agent while a plugin subprocess keeps running — a second run can
reclaim the lease and be acked, and the paused run is abandoned (`stale`) instead
of completing below the new watermark. A finding unique to that pause window can
be missed. This is mitigated, not eliminated: every mutation is fenced on lease
ownership (a reclaimed owner cannot persist-complete, ack, or reset — an atomic
conditional UPDATE, not a check-then-act), the plugin's phase timeouts are kept
under the cron inactivity limit to avoid the orphaned-subprocess route, and
`stale` is surfaced as an operational alert (below), never silently swallowed.

**Nested tool dispatch.** Inside a monitor turn, X/web ride the agent's own
`x_search`/`web_search` through the plugin's `dispatch_tool`, which resolves via
the global tool registry. Credentials, backends, and rate limits are the agent's
— but the nested dispatch is a **trusted-plugin** path and is *not* constrained
by the cron job's `enabled_toolsets` allowlist. The plugin is trusted to reach
X/web regardless of the job allowlist.

Create the job. **Pin `--deliver local`** (do not omit it): the default depends
on how the job was created and can be `origin` (automatic delivery). If automatic
delivery targets the same destination as the manual `hermes send`, Hermes's cron
duplicate-protection turns the manual send into a no-op (`success:true`,
`skipped:true`, exit 0) — the agent would then ack a report the user never saw.
The cron agent needs the `research` (plugin tools) and `terminal` (to run
`hermes send`) toolsets; the `messaging` toolset is deliberately absent from cron
agents, so deliver via the CLI, never the `send_message` tool.

```bash
hermes cron create "0 9 * * 1" \
  "Call last30days_research once (since_last=true, monitor=\"ai-agents\"). \
   If it failed or delta.degraded is true, report briefly and do NOT ack. \
   If delta.status is 'busy', do NOT ack; return exactly [SILENT]. \
   If delta.status is 'stale' or 'missing_previous' (integrity events), \
     hermes send a one-line alert naming the monitor and status to \
     telegram:<chat_id>:<thread_id> and do NOT ack; additionally, only for \
     'missing_previous', call last30days_monitor_reset(monitor, delta.run_id). \
   If delta.status is 'baseline' or delta.counts.new == 0, call \
     last30days_mark_reported(monitor, delta.run_id); return exactly [SILENT]. \
   Otherwise summarize delta.new_findings with their URLs and run \
     'hermes send --json' to telegram:<chat_id>:<thread_id>; ack ONLY if the \
     JSON has success:true, no error, skipped is not true, and a message_id is \
     present — then call last30days_mark_reported(monitor, delta.run_id); \
     then return exactly [SILENT]." \
  --name "monitor: ai-agents" --deliver local --skill last30days
```

Notes:
- **Verify delivery, don't trust exit 0.** A `skipped` (duplicate-protected)
  send is still exit 0. Gate the ack on the `--json` fields: `success:true`,
  no `error`, `skipped` not true, and a `message_id` present. On first install,
  send a one-time *visible* canary so you know the topic target is right.
- **Integrity events alert, they don't hide.** `stale` (a possible bounded-loss
  event) and `missing_previous` (the acked reference run was pruned) must produce
  an operational alert and must **not** advance the watermark — silently
  resetting would make a state-integrity failure look healthy.
- **Toolsets:** `research` + `terminal` are required. `--skill last30days` is
  *optional* — plugin registration plus the `research` toolset already exposes
  `last30days_research`; attach the skill only if you want its instructions in
  context (plugin-qualified as `last30days:last30days`).
- **Target explicitly:** use `telegram:<chat_id>:<thread_id>`; a bare `telegram`
  target routes to the configured home channel, not your monitoring topic.
- **Long runs:** the plugin caps its phases under the cron 600s inactivity limit
  so it self-terminates rather than being orphaned. If a monitor legitimately
  runs longer, raise the job's inactivity limit to at least the 1800s lease TTL,
  or the turn may be interrupted (see *Bounded exception*).
- `[SILENT]` suppresses delivery only on a **successful** final response; a
  failed turn is not silenced (Hermes may deliver its own failure summary if an
  auto-delivery target exists — another reason to pin `--deliver local`).
- The watermark row lives in the store DB at
  `~/.local/share/last30days/research.db` (OS-user scoped). Do **not** set
  `--save-dir` for monitors — it moves the DB and resets the watermark.
- Retire any old external `watchlist.py` cron for the same topic **after** the
  first canary run passes — running both double-runs and double-charges.

## Updating

```bash
hermes skills install mvanhorn/last30days-skill --force
```

If you symlinked your working tree (developer alternative above), just `git pull` in the repo — edits propagate live, no re-install step.

## Support

- Original repo: https://github.com/mvanhorn/last30days-skill
- Hermes: https://github.com/mercurial-tf/hermes
- Issues: Please report in the original repo
