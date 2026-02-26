#!/usr/bin/env python3
"""
Denver Daily Digest — News gathering and summarization script.

Gathers Denver metro news via Brave Search API, fetches article content,
sends to Anthropic API (Claude Sonnet 4.6) for summarization, and writes
a dated JSON file for the static site.
"""

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
MAX_ARTICLES = 10
ARTICLE_TEXT_LIMIT = 3000  # chars per article (increased for better summaries)
ANTHROPIC_MAX_TOKENS = 8000  # hard cap on output tokens
ANTHROPIC_MODEL = "claude-sonnet-4-6"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "site", "data")

# --- Budget Safety Limits ---
MAX_BRAVE_QUERIES_PER_RUN = 15  # hard cap on search API calls per run
MAX_ANTHROPIC_CALLS_PER_RUN = 2  # 1 primary + 1 retry max
brave_query_count = 0
anthropic_call_count = 0

# Brave Search queries
SEARCH_QUERIES = [
    "Denver crime news today",
    "Denver shooting arrest",
    "Denver business economy news today",
    "Denver politics government Colorado legislature",
    "Denver wildfire fire today",
    "Denver traffic accident major incident",
    "Denver development housing construction",
    "Denver metro area police",
    "site:denverpost.com Denver news",
    "site:denvergazette.com Denver news",
    "site:coloradosun.com Colorado news",
    "site:9news.com Denver news",
]

# Preferred sources for article fetching (most reliable HTML)
PREFERRED_SOURCES = [
    "denverpost.com",
    "denvergazette.com",
    "coloradosun.com",
    "cpr.org",
]

# Sources that often block or require JS
UNRELIABLE_SOURCES = [
    "kdvr.com",
    "foxnews.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "reddit.com",
]

# --- Denver/Colorado Geographic Relevance ---
# Stories must mention at least one of these terms to be included.
# Comprehensive list of Denver metro cities, counties, landmarks, and Colorado terms.
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
    # Denver neighborhoods often mentioned by name
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
]

# Request headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DenverDigestBot/1.0)"
}


def check_denver_time():
    """Exit early if Denver local time is before 7 AM (DST handling)."""
    now = datetime.datetime.now(DENVER_TZ)
    if now.hour < 7:
        print(f"Denver time is {now.strftime('%H:%M %Z')} -- too early, skipping.")
        sys.exit(0)


def check_already_generated():
    """Exit if today's digest file already exists."""
    today = datetime.datetime.now(DENVER_TZ).strftime("%Y-%m-%d")
    filepath = os.path.join(OUTPUT_DIR, f"{today}.json")
    if os.path.exists(filepath):
        print(f"Today's digest ({today}.json) already exists. Skipping.")
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


def brave_search(query, api_key, count=10):
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
        "freshness": "pd",  # past day
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


def gather_search_results(api_key):
    """Run all search queries and collect results."""
    all_results = []
    for query in SEARCH_QUERIES:
        print(f"  Searching: {query}")
        results = brave_search(query, api_key)
        all_results.extend(results)
        time.sleep(0.3)  # be polite to the API
    print(f"  Total raw results: {len(all_results)}")
    return all_results


def deduplicate_and_rank(results):
    """
    Group similar results by headline similarity and keyword overlap.
    Filter out non-Denver stories. Rank by source coverage.
    Return top MAX_ARTICLES stories.
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
    # Extract significant words for each group's representative headline
    group_keywords = []
    for group in groups:
        # Combine all headlines in the group for keyword extraction
        all_titles = " ".join(r["title"] for r in group)
        keywords = extract_significant_words(all_titles)
        group_keywords.append(keywords)

    # Merge groups that share 3+ significant words
    merged = [True] * len(groups)  # track which groups are still active
    for i in range(len(groups)):
        if not merged[i]:
            continue
        for j in range(i + 1, len(groups)):
            if not merged[j]:
                continue
            shared = group_keywords[i] & group_keywords[j]
            if len(shared) >= 3:
                # Merge group j into group i
                groups[i].extend(groups[j])
                group_keywords[i] = group_keywords[i] | group_keywords[j]
                merged[j] = False
                print(f"  Merged duplicate stories: '{groups[j][0]['title'][:60]}...' into existing group")

    # Keep only active groups
    active_groups = [g for g, m in zip(groups, merged) if m]

    # Score each group by number of unique sources
    scored = []
    for group in active_groups:
        sources = set(r["source"] for r in group)
        # Prefer groups from preferred sources
        preferred_count = sum(
            1 for s in sources
            if any(p in s.lower() for p in ["denver post", "colorado sun", "gazette", "cpr"])
        )
        score = len(sources) * 2 + preferred_count
        # Pick the best representative (prefer preferred sources)
        best = group[0]
        for r in group:
            if any(p in r["url"] for p in PREFERRED_SOURCES):
                best = r
                break
        scored.append((score, best, group))

    # Sort by score descending, take top MAX_ARTICLES
    scored.sort(key=lambda x: x[0], reverse=True)
    top_stories = []
    for score, best, group in scored[:MAX_ARTICLES]:
        # Combine snippet text from all sources in the group
        all_snippets = " ".join(r["snippet"] for r in group if r["snippet"])
        best["combined_snippets"] = all_snippets
        best["source_count"] = len(set(r["source"] for r in group))
        top_stories.append(best)

    print(f"  Selected {len(top_stories)} stories after dedup/ranking")
    return top_stories


def fetch_article_text(url):
    """Fetch and extract article body text from a URL."""
    # Skip unreliable sources
    if any(s in url for s in UNRELIABLE_SOURCES):
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove script, style, nav, header, footer, aside elements
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "figure", "figcaption"]):
            tag.decompose()

        # Try common article body selectors
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

        # Extract paragraph text
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


def build_prompt(stories, today_str):
    """Build the user prompt for the Anthropic API call."""
    story_blocks = []
    for i, story in enumerate(stories, 1):
        block = f"""[STORY {i}]
Headline: {story['title']}
Source: {story['source']}
URL: {story['url']}
Article text: {story['article_text']}"""
        story_blocks.append(block)

    stories_text = "\n\n".join(story_blocks)

    return f"""Today is {today_str}. Below are the top Denver metro news stories from local sources.

{stories_text}

---

Produce a JSON array of exactly {len(stories)} stories. For each story:
- "category": one of "crime", "business", "politics", or "other"
- "headline": clear, factual headline
- "summary": 3-4 paragraphs, each 2-4 sentences. Use \\n\\n between paragraphs. Cover what happened, who is involved, where, why it matters.
- "source": publication name
- "url": direct link to the original article

Return ONLY the JSON array, no other text."""


SYSTEM_PROMPT = """You are a news editor producing a daily digest for Denver, Colorado. For each story, write a factual, detailed summary in 3-4 paragraphs. Clean newspaper style. No editorializing, no emojis."""


def call_anthropic(stories, today_str):
    """Send stories to Anthropic API and get summarized JSON back."""
    global anthropic_call_count

    client = anthropic.Anthropic()
    user_prompt = build_prompt(stories, today_str)

    print(f"\n--- Anthropic API Call ---")
    print(f"  Model: {ANTHROPIC_MODEL}")
    print(f"  Max output tokens: {ANTHROPIC_MAX_TOKENS}")

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


def write_output(stories_json, today_str, today_formatted):
    """Write the final JSON data file."""
    output = {
        "date": today_str,
        "dateFormatted": today_formatted,
        "stories": stories_json,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, f"{today_str}.json")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(stories_json)} stories to {filepath}")
    return filepath


def main():
    print("=" * 60)
    print("Denver Daily Digest -- Generate")
    print("=" * 60)

    # Check Denver time (DST-aware)
    check_denver_time()

    # Check if already generated today
    check_already_generated()

    now = datetime.datetime.now(DENVER_TZ)
    today_str = now.strftime("%Y-%m-%d")
    today_formatted = now.strftime("%A, %B %d, %Y").replace(" 0", " ")
    print(f"Date: {today_formatted} ({today_str})")
    print(f"Denver time: {now.strftime('%H:%M %Z')}")

    # Verify API keys
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not brave_key:
        print("ERROR: BRAVE_SEARCH_API_KEY not set")
        sys.exit(1)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Step 1: Search
    print("\n[Step 1] Searching for Denver news...")
    raw_results = gather_search_results(brave_key)
    if not raw_results:
        print("ERROR: No search results found")
        sys.exit(1)

    # Step 2: Deduplicate and rank
    print("\n[Step 2] Deduplicating and ranking stories...")
    top_stories = deduplicate_and_rank(raw_results)
    if not top_stories:
        print("ERROR: No stories after dedup")
        sys.exit(1)

    # Step 3: Fetch article content
    print("\n[Step 3] Fetching article content...")
    stories_with_text = fetch_articles(top_stories)

    # Step 4: Summarize via Anthropic
    print("\n[Step 4] Sending to Anthropic API for summarization...")
    summaries = call_anthropic(stories_with_text, today_str)

    # Step 5: Write output
    print("\n[Step 5] Writing JSON output...")
    filepath = write_output(summaries, today_str, today_formatted)

    print("\nDone!")
    return filepath


if __name__ == "__main__":
    main()
