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

## Updating

```bash
hermes skills install mvanhorn/last30days-skill --force
```

If you symlinked your working tree (developer alternative above), just `git pull` in the repo — edits propagate live, no re-install step.

## Support

- Original repo: https://github.com/mvanhorn/last30days-skill
- Hermes: https://github.com/mercurial-tf/hermes
- Issues: Please report in the original repo
