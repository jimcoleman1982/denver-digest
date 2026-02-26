#!/usr/bin/env python3
"""
303 News -- News gathering and summarization script.

Gathers Denver metro news via Brave Search API, fetches article content,
sends to Anthropic API (Claude Sonnet 4.6) for curation and summarization,
and writes a dated JSON file for the static site.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import anthropic
import requests
from bs4 import BeautifulSoup

# --- Configuration ---
DENVER_TZ = ZoneInfo("America/Denver")
MAX_CANDIDATES = 25  # gather this many before curation
MAX_ARTICLES = 12    # final output after Claude curation
ARTICLE_TEXT_LIMIT = 3000  # chars per article
ANTHROPIC_MAX_TOKENS = 10000  # hard cap on output tokens
ANTHROPIC_MODEL = "claude-sonnet-4-6"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "site", "data")
SITE_URL = "https://303news.org"

# Category display order and labels (must match site JavaScript)
CATEGORY_ORDER = ["other", "politics", "business", "crime", "sports"]
CATEGORY_LABELS = {
    "other": "GENERAL / METRO NEWS",
    "politics": "POLITICS & GOVERNMENT",
    "business": "BUSINESS & ECONOMY",
    "crime": "CRIME & PUBLIC SAFETY",
    "sports": "SPORTS",
}

# --- Budget Safety Limits ---
MAX_BRAVE_QUERIES_PER_RUN = 20  # hard cap on search API calls per run
MAX_ANTHROPIC_CALLS_PER_RUN = 2  # 1 primary + 1 retry max
brave_query_count = 0
anthropic_call_count = 0

# Brave Search queries -- covers all categories including sports
SEARCH_QUERIES = [
    # General / Metro
    "Denver metro news today",
    "Denver wildfire fire today",
    "Denver traffic accident major incident",
    "Denver development housing construction",
    # Crime
    "Denver crime news today",
    "Denver shooting arrest",
    "Denver metro area police",
    # Business
    "Denver business economy news today",
    "Denver Business Journal news",
    # Politics
    "Denver politics government Colorado legislature",
    "Colorado governor Polis legislation",
    # Sports
    "Denver Broncos NFL news",
    "Denver Nuggets NBA news",
    "Colorado Avalanche NHL news",
    # Site-specific
    "site:denverpost.com Denver news",
    "site:denvergazette.com Denver news",
    "site:coloradosun.com Colorado news",
]

# Preferred sources for article fetching (most reliable HTML)
PREFERRED_SOURCES = [
    "denverpost.com",
    "denvergazette.com",
    "coloradosun.com",
    "cpr.org",
    "bizjournals.com",
]

# Sources that often block or require JS
UNRELIABLE_SOURCES = [
    "foxnews.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "reddit.com",
    "tiktok.com",
    "instagram.com",
]

# --- Denver/Colorado Geographic Relevance ---
DENVER_METRO_KEYWORDS = [
    # Denver and major suburbs
    "denver", "aurora", "lakewood", "westminster", "thornton", "arvada",
    "centennial", "broomfield", "brighton", "commerce city",
    "englewood", "federal heights", "golden", "northglenn", "wheat ridge",
    # South metro
    "littleton", "lone tree", "parker", "castle rock", "castle pines",
    "highlands ranch", "greenwood village", "cherry hills village",
    "columbine", "roxborough", "sedalia", "larkspur", "ken caryl",
    # East metro
    "glendale", "sheridan", "foxfield", "deer trail", "bennett",
    # Boulder / northwest metro
    "boulder", "louisville", "superior", "lafayette", "longmont",
    "nederland", "lyons", "niwot",
    # Mountain communities (west metro)
    "evergreen", "conifer", "morrison", "idaho springs", "georgetown",
    "black hawk", "central city", "edgewater",
    # North metro / Weld County
    "erie", "firestone", "frederick", "dacono", "fort lupton",
    "greeley", "windsor", "timnath", "wellington", "mead", "johnstown",
    # Larimer County
    "fort collins", "loveland", "berthoud", "estes park",
    # Colorado Springs / south
    "colorado springs", "monument", "pueblo", "fountain",
    # Denver neighborhoods
    "lodo", "rino", "five points", "capitol hill", "central park",
    "stapleton", "park hill", "cherry creek", "montbello",
    "green valley ranch", "globeville", "elyria", "swansea",
    "sun valley", "baker", "wash park", "washington park",
    "sloan", "sloans lake", "highland", "berkeley", "barnum",
    # Counties
    "arapahoe county", "jefferson county", "adams county", "douglas county",
    "el paso county", "weld county", "larimer county", "boulder county",
    "broomfield county", "clear creek county", "gilpin county",
    "park county", "elbert county",
    # State and regional terms
    "colorado", "front range", "western slope", "eastern plains",
    # Major roads and landmarks
    "i-25", "i-70", "i-76", "i-225", "i-270", "c-470", "e-470",
    "dia", "denver international", "coors field", "empower field",
    "ball arena", "red rocks", "rocky mountain", "mile high",
    "16th street mall", "union station",
    # Sports teams (always Denver-relevant)
    "broncos", "nuggets", "avalanche", "colorado rockies", "rapids",
]

# Request headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; 303NewsBot/1.0)"
}


def parse_args():
    """Parse command line arguments for backfill support."""
    parser = argparse.ArgumentParser(description="303 News digest generator")
    parser.add_argument(
        "--date",
        help="Generate digest for a specific date (YYYY-MM-DD). Used for backfill.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing digest file for the target date.",
    )
    return parser.parse_args()


def check_denver_time():
    """Exit early if Denver local time is before 6 AM (DST handling)."""
    now = datetime.datetime.now(DENVER_TZ)
    if now.hour < 6:
        print(f"Denver time is {now.strftime('%H:%M %Z')} -- too early, skipping.")
        sys.exit(0)


def check_already_generated(date_str):
    """Exit if the digest file already exists for the given date."""
    filepath = os.path.join(OUTPUT_DIR, f"{date_str}.json")
    if os.path.exists(filepath):
        print(f"Digest for {date_str} already exists. Skipping. (Use --force to overwrite)")
        sys.exit(0)


def is_denver_relevant(title, snippet):
    """Check if a story is relevant to the Denver/Colorado metro area."""
    text = (title + " " + snippet).lower()
    return any(keyword in text for keyword in DENVER_METRO_KEYWORDS)


def extract_significant_words(text):
    """Extract significant words from text for keyword-based dedup."""
    stop_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "has", "had", "have", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "this", "that", "these", "those",
        "it", "its", "his", "her", "he", "she", "they", "them", "their",
        "our", "we", "you", "your", "my", "me", "who", "what", "when",
        "where", "how", "why", "not", "no", "all", "each", "every", "both",
        "few", "more", "most", "other", "some", "such", "than", "too", "very",
        "just", "about", "after", "before", "new", "first", "last", "over",
        "into", "also", "back", "up", "out", "says", "said", "news", "today",
        "area", "metro", "county", "city", "state", "report", "reports",
    }
    words = re.findall(r'[a-z]+', text.lower())
    return set(w for w in words if len(w) >= 3 and w not in stop_words)


def brave_search(query, api_key, count=10, freshness="pd"):
    """Run a single Brave Search API query. Returns list of result dicts."""
    global brave_query_count
    brave_query_count += 1
    if brave_query_count > MAX_BRAVE_QUERIES_PER_RUN:
        print(f"  LIMIT: Brave query cap ({MAX_BRAVE_QUERIES_PER_RUN}) reached. Skipping.")
        return []

    url = "https://api.search.brave.com/res/v1/web/search"
    params = {
        "q": query,
        "count": count,
        "freshness": freshness,
    }
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
                "source": extract_source_name(item.get("url", "")),
            })
        return results
    except Exception as e:
        print(f"  Search failed for '{query}': {e}")
        return []


def extract_source_name(url):
    """Extract a readable source name from a URL."""
    source_map = {
        "denverpost.com": "Denver Post",
        "denvergazette.com": "Denver Gazette",
        "coloradosun.com": "Colorado Sun",
        "cpr.org": "CPR News",
        "9news.com": "9News",
        "kdvr.com": "Fox31 Denver",
        "denver7.com": "Denver7",
        "cbsnews.com": "CBS News Colorado",
        "denverite.com": "Denverite",
        "bizjournals.com": "Denver Business Journal",
        "espn.com": "ESPN",
        "theathletic.com": "The Athletic",
        "nytimes.com": "New York Times",
        "apnews.com": "Associated Press",
    }
    for domain, name in source_map.items():
        if domain in url:
            return name
    # Fallback: extract domain
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except Exception:
        return "Unknown"


def gather_search_results(api_key, freshness="pd"):
    """Run all search queries and collect results."""
    all_results = []
    for query in SEARCH_QUERIES:
        print(f"  Searching: {query}")
        results = brave_search(query, api_key, freshness=freshness)
        all_results.extend(results)
        time.sleep(0.3)  # be polite to the API
    print(f"  Total raw results: {len(all_results)}")
    return all_results


def deduplicate_and_rank(results):
    """
    Group similar results by headline similarity and keyword overlap.
    Filter out non-Denver stories. Rank by source coverage.
    Return top MAX_CANDIDATES stories for Claude to curate.
    """
    # Filter out unreliable sources
    filtered = [r for r in results if not any(s in r["url"] for s in UNRELIABLE_SOURCES)]

    # Filter out non-Denver/Colorado stories
    denver_only = []
    rejected_count = 0
    for r in filtered:
        if is_denver_relevant(r["title"], r["snippet"]):
            denver_only.append(r)
        else:
            rejected_count += 1
    if rejected_count > 0:
        print(f"  Filtered out {rejected_count} non-Denver stories")
    filtered = denver_only

    # Remove exact URL duplicates
    seen_urls = set()
    unique = []
    for r in filtered:
        normalized = r["url"].split("?")[0].rstrip("/")
        if normalized not in seen_urls:
            seen_urls.add(normalized)
            unique.append(r)

    # --- Pass 1: Group by headline similarity (SequenceMatcher) ---
    groups = []
    used = set()

    for i, item in enumerate(unique):
        if i in used:
            continue
        group = [item]
        used.add(i)
        for j, other in enumerate(unique):
            if j in used:
                continue
            similarity = SequenceMatcher(
                None, item["title"].lower(), other["title"].lower()
            ).ratio()
            if similarity > 0.5:
                group.append(other)
                used.add(j)
        groups.append(group)

    # --- Pass 2: Merge groups that share the same event (keyword overlap) ---
    group_keywords = []
    for group in groups:
        all_titles = " ".join(r["title"] for r in group)
        keywords = extract_significant_words(all_titles)
        group_keywords.append(keywords)

    merged = [True] * len(groups)
    for i in range(len(groups)):
        if not merged[i]:
            continue
        for j in range(i + 1, len(groups)):
            if not merged[j]:
                continue
            shared = group_keywords[i] & group_keywords[j]
            if len(shared) >= 3:
                groups[i].extend(groups[j])
                group_keywords[i] = group_keywords[i] | group_keywords[j]
                merged[j] = False
                print(f"  Merged duplicate stories: '{groups[j][0]['title'][:60]}...' into existing group")

    active_groups = [g for g, m in zip(groups, merged) if m]

    # Score each group by number of unique sources
    scored = []
    for group in active_groups:
        sources = set(r["source"] for r in group)
        preferred_count = sum(
            1 for s in sources
            if any(p in s.lower() for p in ["denver post", "colorado sun", "gazette", "cpr", "business journal"])
        )
        score = len(sources) * 2 + preferred_count
        # Pick the best representative (prefer preferred sources)
        best = group[0]
        for r in group:
            if any(p in r["url"] for p in PREFERRED_SOURCES):
                best = r
                break
        scored.append((score, best, group))

    # Sort by score descending, take top MAX_CANDIDATES
    scored.sort(key=lambda x: x[0], reverse=True)
    top_stories = []
    for score, best, group in scored[:MAX_CANDIDATES]:
        all_snippets = " ".join(r["snippet"] for r in group if r["snippet"])
        best["combined_snippets"] = all_snippets
        best["source_count"] = len(set(r["source"] for r in group))
        top_stories.append(best)

    print(f"  Selected {len(top_stories)} candidate stories after dedup/ranking")
    return top_stories


def load_recent_headlines(target_date_str, days_back=3):
    """Load headlines from previous days' JSON files for cross-day dedup."""
    target = datetime.date.fromisoformat(target_date_str)
    previous_headlines = []
    for i in range(1, days_back + 1):
        prev_date = target - datetime.timedelta(days=i)
        prev_file = os.path.join(OUTPUT_DIR, f"{prev_date.isoformat()}.json")
        if os.path.exists(prev_file):
            try:
                with open(prev_file) as f:
                    data = json.load(f)
                for story in data.get("stories", []):
                    previous_headlines.append(story.get("headline", "").lower())
            except (json.JSONDecodeError, KeyError):
                continue
    return previous_headlines


def filter_cross_day_duplicates(candidates, target_date_str):
    """Remove candidates whose headlines are too similar to recent days' stories."""
    previous_headlines = load_recent_headlines(target_date_str)
    if not previous_headlines:
        print("  No previous days' data found -- skipping cross-day dedup")
        return candidates

    print(f"  Checking against {len(previous_headlines)} headlines from previous days")
    kept = []
    removed = 0
    for candidate in candidates:
        title_lower = candidate["title"].lower()
        is_duplicate = False
        for prev_headline in previous_headlines:
            similarity = SequenceMatcher(None, title_lower, prev_headline).ratio()
            if similarity > 0.55:
                is_duplicate = True
                print(f"  Cross-day dup removed: '{candidate['title'][:60]}...'")
                break
            # Also check keyword overlap
            candidate_words = extract_significant_words(title_lower)
            prev_words = extract_significant_words(prev_headline)
            shared = candidate_words & prev_words
            if len(shared) >= 4:
                is_duplicate = True
                print(f"  Cross-day dup removed (keywords): '{candidate['title'][:60]}...'")
                break
        if is_duplicate:
            removed += 1
        else:
            kept.append(candidate)

    if removed > 0:
        print(f"  Removed {removed} cross-day duplicates, {len(kept)} candidates remain")
    return kept


def fetch_article_text(url):
    """Fetch and extract article body text from a URL."""
    if any(s in url for s in UNRELIABLE_SOURCES):
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "figure", "figcaption"]):
            tag.decompose()

        article_body = None
        selectors = [
            "article",
            '[class*="article-body"]',
            '[class*="story-body"]',
            '[class*="entry-content"]',
            '[class*="post-content"]',
            '[class*="article-content"]',
            "main",
        ]
        for selector in selectors:
            found = soup.select_one(selector)
            if found:
                article_body = found
                break

        if not article_body:
            article_body = soup.body if soup.body else soup

        paragraphs = article_body.find_all("p")
        text = "\n".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30)

        if len(text) < 100:
            return None

        return text[:ARTICLE_TEXT_LIMIT]

    except Exception as e:
        print(f"  Failed to fetch {url}: {e}")
        return None


def fetch_articles(stories):
    """Fetch article text for each story. Falls back to snippets."""
    for story in stories:
        print(f"  Fetching: {story['url']}")
        text = fetch_article_text(story["url"])
        if text:
            story["article_text"] = text
            print(f"    Got {len(text)} chars of article text")
        else:
            story["article_text"] = story.get("combined_snippets", story.get("snippet", ""))
            print(f"    Using snippet text ({len(story['article_text'])} chars)")
    return stories


def build_prompt(stories, target_date_str):
    """Build the user prompt for the Anthropic API call with curation instructions."""
    story_blocks = []
    for i, story in enumerate(stories, 1):
        block = f"""[CANDIDATE {i}]
Headline: {story['title']}
Source: {story['source']}
URL: {story['url']}
Article text: {story['article_text']}"""
        story_blocks.append(block)

    stories_text = "\n\n".join(story_blocks)

    return f"""Date: {target_date_str}. Below are {len(stories)} candidate Denver metro news stories.

YOUR TASK: Select the top {MAX_ARTICLES} stories and write summaries. Use editorial judgment:
- Prioritize stories with the highest impact on Denver metro residents
- Ensure category balance: aim for at least 1-2 stories per category (other, politics, business, crime, sports)
- Prefer stories from credible local sources (Denver Post, Colorado Sun, Denver Gazette, CPR News, Denver Business Journal)
- Drop stories that are trivial, clickbait, or national news with weak Denver relevance
- If two candidates cover the same event, pick the one with better sourcing

{stories_text}

---

Produce a JSON array of exactly {MAX_ARTICLES} stories. For each story:
- "category": one of "crime", "business", "politics", "sports", or "other"
- "headline": clear, factual headline
- "summary": 3-4 paragraphs, each 2-4 sentences. Use \\n\\n between paragraphs. Cover what happened, who is involved, where, why it matters.
- "source": publication name
- "url": direct link to the original article

Return ONLY the JSON array, no other text."""


SYSTEM_PROMPT = """You are the editor-in-chief of 303 News, a daily digest for the Denver, Colorado metro area. You select and summarize the most important local stories each day. Write factual, detailed summaries in clean newspaper style. No editorializing, no emojis. Categories: crime, business, politics, sports, other."""


def call_anthropic(stories, target_date_str):
    """Send stories to Anthropic API and get curated, summarized JSON back."""
    global anthropic_call_count

    client = anthropic.Anthropic()
    user_prompt = build_prompt(stories, target_date_str)

    print(f"\n--- Anthropic API Call ---")
    print(f"  Model: {ANTHROPIC_MODEL}")
    print(f"  Max output tokens: {ANTHROPIC_MAX_TOKENS}")
    print(f"  Candidates sent: {len(stories)}")
    print(f"  Target output: {MAX_ARTICLES} stories")

    anthropic_call_count += 1
    if anthropic_call_count > MAX_ANTHROPIC_CALLS_PER_RUN:
        print(f"  LIMIT: Anthropic call cap ({MAX_ANTHROPIC_CALLS_PER_RUN}) reached. Exiting.")
        sys.exit(1)

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        print(f"  API call failed: {e}")
        print("  Retrying in 30 seconds...")
        time.sleep(30)
        anthropic_call_count += 1
        if anthropic_call_count > MAX_ANTHROPIC_CALLS_PER_RUN:
            print(f"  LIMIT: Anthropic call cap ({MAX_ANTHROPIC_CALLS_PER_RUN}) reached. Exiting.")
            sys.exit(1)
        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e2:
            print(f"  Retry failed: {e2}")
            sys.exit(1)

    # Log token usage
    usage = response.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000
    print(f"  Input tokens:  {input_tokens:,}")
    print(f"  Output tokens: {output_tokens:,}")
    print(f"  Estimated cost: ${cost:.4f}")
    print(f"  Stop reason: {response.stop_reason}")

    # Extract text content
    raw_text = response.content[0].text

    # Try to parse JSON directly
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from markdown code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find a JSON array in the text
    match = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    print(f"  Failed to parse JSON from response. Raw text:\n{raw_text[:500]}")
    sys.exit(1)


def write_output(stories_json, date_str, date_formatted):
    """Write the final JSON data file."""
    output = {
        "date": date_str,
        "dateFormatted": date_formatted,
        "stories": stories_json,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, f"{date_str}.json")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(stories_json)} stories to {filepath}")
    return filepath


def get_freshness_for_date(target_date):
    """Determine Brave Search freshness parameter for a given date."""
    now = datetime.datetime.now(DENVER_TZ).date()
    delta = (now - target_date).days

    if delta <= 0:
        return "pd"   # past day (today)
    elif delta <= 7:
        return "pw"   # past week
    else:
        return "pm"   # past month


def build_email_html(stories_json, date_str, date_formatted):
    """Build an HTML email with headlines, teasers, and deep links."""
    # Group stories by category in display order
    groups = {}
    for story in stories_json:
        cat = story.get("category", "other")
        if cat not in CATEGORY_LABELS:
            cat = "other"
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(story)

    # Build a global index matching the site's rendering order
    story_index = 0
    sections_html = ""

    for cat in CATEGORY_ORDER:
        if cat not in groups or not groups[cat]:
            continue
        label = CATEGORY_LABELS[cat]
        sections_html += f'''
        <tr><td style="padding: 20px 0 8px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 14px; font-weight: 700; letter-spacing: 2px; color: #444; text-transform: uppercase; border-bottom: 1px solid #ddd;">{label}</td></tr>
'''
        for story in groups[cat]:
            headline = story.get("headline", "")
            summary = story.get("summary", "")
            source = story.get("source", "")
            # Get first sentence of first paragraph as teaser
            first_para = summary.split("\n\n")[0] if summary else ""
            sentences = re.split(r'(?<=[.!?])\s+', first_para)
            teaser = sentences[0] if sentences else first_para[:150]
            if len(teaser) > 200:
                teaser = teaser[:197] + "..."

            deep_link = f"{SITE_URL}/#story-{date_str}-{story_index}"

            sections_html += f'''
        <tr><td style="padding: 14px 0 2px 0;">
            <a href="{deep_link}" style="font-family: Georgia, 'Times New Roman', serif; font-size: 18px; color: #1a3a6a; text-decoration: none; line-height: 1.4;">{headline}</a>
        </td></tr>
        <tr><td style="padding: 0 0 8px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 15px; color: #555; line-height: 1.5;">
            {teaser} <span style="font-size: 13px; color: #888; font-style: italic;">-- {source}</span>
        </td></tr>
'''
            story_index += 1

    html = f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; background-color: #f5f0e8;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f5f0e8;">
<tr><td align="center" style="padding: 12px 8px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width: 720px;">
    <tr><td style="text-align: center; padding: 24px 0 20px 0; border-bottom: 3px double #1a1a1a;">
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 32px; font-weight: 700; letter-spacing: 3px;"><span style="color: #8b1a1a;">303</span> <span style="color: #1a1a1a;">NEWS</span></div>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 14px; font-style: italic; color: #666; margin-top: 4px;">{date_formatted}</div>
    </td></tr>
{sections_html}
    <tr><td style="padding: 28px 0 16px 0; text-align: center; border-top: 1px solid #ccc;">
        <a href="{SITE_URL}" style="font-family: Georgia, 'Times New Roman', serif; font-size: 15px; color: #1a3a6a; text-decoration: none;">Read full summaries at 303news.org</a>
    </td></tr>
    <tr><td style="text-align: center; padding: 0 0 20px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 12px; color: #999; font-style: italic;">
        No ads and no BS, just clean daily news.
    </td></tr>
</table>
</td></tr>
</table>
</body>
</html>'''
    return html


def send_email(stories_json, date_str, date_formatted):
    """Send the daily digest email via Buttondown API."""
    buttondown_key = os.environ.get("BUTTONDOWN_API_KEY")
    if not buttondown_key:
        print("  BUTTONDOWN_API_KEY not set. Skipping email.")
        return

    email_html = build_email_html(stories_json, date_str, date_formatted)
    subject = f"303 News -- {date_formatted}"

    print(f"\n--- Sending Email ---")
    print(f"  Subject: {subject}")

    try:
        resp = requests.post(
            "https://api.buttondown.com/v1/emails",
            headers={
                "Authorization": f"Token {buttondown_key}",
                "Content-Type": "application/json",
            },
            json={
                "subject": subject,
                "body": email_html,
                "status": "about_to_send",
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print("  Email sent successfully.")
        else:
            print(f"  Email send failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        print(f"  Email send error: {e}")


def main():
    args = parse_args()

    print("=" * 60)
    print("303 News -- Generate Digest")
    print("=" * 60)

    # Determine target date
    if args.date:
        try:
            target_date = datetime.date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: Invalid date format '{args.date}'. Use YYYY-MM-DD.")
            sys.exit(1)
        target_date_str = args.date
        target_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, tzinfo=DENVER_TZ)
        target_formatted = target_dt.strftime("%A, %B %d, %Y").replace(" 0", " ")
        print(f"Backfill mode: generating for {target_formatted}")
    else:
        # Normal daily mode
        check_denver_time()
        now = datetime.datetime.now(DENVER_TZ)
        target_date = now.date()
        target_date_str = now.strftime("%Y-%m-%d")
        target_formatted = now.strftime("%A, %B %d, %Y").replace(" 0", " ")
        print(f"Date: {target_formatted} ({target_date_str})")
        print(f"Denver time: {now.strftime('%H:%M %Z')}")

    # Check if already generated (skip in force mode)
    if not args.force:
        check_already_generated(target_date_str)

    # Verify API keys
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not brave_key:
        print("ERROR: BRAVE_SEARCH_API_KEY not set")
        sys.exit(1)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Determine search freshness based on target date
    freshness = get_freshness_for_date(target_date)
    print(f"Search freshness: {freshness}")

    # Step 1: Search
    print("\n[Step 1] Searching for Denver news...")
    raw_results = gather_search_results(brave_key, freshness=freshness)
    if not raw_results:
        print("ERROR: No search results found")
        sys.exit(1)

    # Step 2: Deduplicate and rank
    print("\n[Step 2] Deduplicating and ranking stories...")
    top_stories = deduplicate_and_rank(raw_results)
    if not top_stories:
        print("ERROR: No stories after dedup")
        sys.exit(1)

    # Step 2b: Remove stories that appeared in previous days
    print("\n[Step 2b] Filtering cross-day duplicates...")
    top_stories = filter_cross_day_duplicates(top_stories, target_date_str)
    if not top_stories:
        print("ERROR: No stories after cross-day dedup")
        sys.exit(1)

    # Step 3: Fetch article content
    print("\n[Step 3] Fetching article content...")
    stories_with_text = fetch_articles(top_stories)

    # Step 4: Curate and summarize via Anthropic
    print(f"\n[Step 4] Sending {len(stories_with_text)} candidates to Anthropic for curation...")
    summaries = call_anthropic(stories_with_text, target_date_str)

    # Step 5: Write output
    print("\n[Step 5] Writing JSON output...")
    filepath = write_output(summaries, target_date_str, target_formatted)

    # Step 6: Send email (skip for backfill unless forced)
    if not args.date:
        print("\n[Step 6] Sending newsletter email...")
        send_email(summaries, target_date_str, target_formatted)
    else:
        print("\n[Step 6] Skipping email (backfill mode).")

    print("\nDone!")
    return filepath


if __name__ == "__main__":
    main()
