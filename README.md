# Denver Daily Digest

Automated daily news digest for the Denver metro area. Runs entirely on GitHub infrastructure.

Every day at 7:00 AM Denver local time, a GitHub Actions workflow gathers the top 10 Denver news stories, summarizes them via the Anthropic API (Claude Sonnet 4.6), and publishes them to a static site hosted on GitHub Pages.

## How It Works

1. **GitHub Actions** fires a cron job daily at 7 AM MT (handles MST/MDT automatically)
2. **Python script** searches for Denver news via Brave Search API
3. Fetches article content from Denver Post, Colorado Sun, Denver Gazette, CPR News, and others
4. Sends articles to **Claude Sonnet 4.6** for categorization and multi-paragraph summarization
5. Writes a dated JSON file to `site/data/`
6. Commits and pushes -- **GitHub Pages** auto-deploys the updated site

## Setup

### Prerequisites

1. **GitHub account** with a private repo
2. **Anthropic API key** -- [console.anthropic.com](https://console.anthropic.com/) (create account, go to API Keys, create key)
3. **Brave Search API key** -- [brave.com/search/api](https://brave.com/search/api/) (sign up for free plan: 2,000 queries/month)

### Step-by-Step Setup

1. **Create a private GitHub repo** named `denver-digest`

2. **Push all project files** to the repo

3. **Add API key secrets:**
   - Go to repo **Settings** > **Secrets and variables** > **Actions**
   - Click **New repository secret**
   - Add `ANTHROPIC_API_KEY` with your Anthropic key
   - Add `BRAVE_SEARCH_API_KEY` with your Brave Search key

4. **Enable GitHub Pages:**
   - Go to repo **Settings** > **Pages**
   - Source: **Deploy from a branch**
   - Branch: `main`
   - Folder: `/site`
   - Click **Save**

5. **Test it:**
   - Go to the **Actions** tab
   - Select **Daily Denver Digest**
   - Click **Run workflow** > **Run workflow**
   - Watch the logs -- you'll see token usage and cost reported
   - After it completes, check `site/data/` for the new JSON file
   - Visit your Pages URL to see the site

## Cost Estimate

| Service | Cost |
|---------|------|
| GitHub Actions | Free (well within 2,000 min/month free tier) |
| GitHub Pages | Free |
| Anthropic API | ~$0.10-$0.30/day (Sonnet 4.6, ~5K input + ~3K output tokens) |
| Brave Search API | Free (free tier: 2,000 queries/month, uses ~300/month) |
| **Monthly total** | **~$3-$9/month** |

Token usage is logged in every run. Check GitHub Actions logs to see exact costs.

## Architecture

```
denver-digest/
├── .github/workflows/daily-digest.yml   (cron workflow, DST-aware)
├── scripts/generate_digest.py           (news search + summarize + JSON)
├── site/
│   ├── index.html                       (static site, single file)
│   └── data/                            (daily JSON, 14-day rolling)
├── requirements.txt
└── README.md
```

## Budget Safety

- `max_tokens=4000` hard cap on every API call
- Single API call per run (no loops)
- One retry on failure, then exits
- Token usage and cost logged in every run
- Article text truncated to 1,500 chars each to minimize input tokens
