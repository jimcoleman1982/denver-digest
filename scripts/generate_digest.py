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
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
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
    "other": "DENVER METRO NEWS",
    "politics": "POLITICS & GOVERNMENT",
    "business": "BUSINESS & ECONOMY",
    "crime": "CRIME & PUBLIC SAFETY",
    "sports": "DENVER SPORTS",
}

# --- Budget Safety Limits ---
MAX_BRAVE_QUERIES_PER_RUN = 55  # hard cap on search API calls per run
MAX_ANTHROPIC_CALLS_PER_RUN = 6  # news + weather + events (Fri) + top story + retries
brave_query_count = 0
anthropic_call_count = 0

# Brave Search queries -- covers all categories including sports
SEARCH_QUERIES = [
    # General / Metro
    "Denver metro news today",
    "Colorado news today",
    "Denver wildfire fire today",
    "Denver traffic accident major incident",
    "Denver development housing construction",
    "Denver weather Colorado forecast",
    # Crime
    "Denver crime news today",
    "Denver shooting arrest",
    "Denver metro area police",
    # Business
    "Denver business economy news today",
    "Denver Business Journal news",
    "Denver restaurant food opening closing",
    # Politics
    "Denver politics government Colorado legislature",
    "Colorado governor Polis legislation",
    "Denver mayor city council",
    # Education / Community
    "Colorado education schools Denver",
    "Denver transportation RTD transit",
    # Sports
    "Denver Broncos NFL news",
    "Denver Nuggets NBA news",
    "Colorado Avalanche NHL news",
    "Colorado Rockies MLB news",
    # Site-specific (newspapers)
    "site:denverpost.com Denver news",
    "site:denvergazette.com Denver news",
    "site:coloradosun.com Colorado news",
    # Site-specific (TV stations)
    "site:9news.com Denver news",
    "site:denver7.com Denver Colorado news",
    "site:kdvr.com Denver Colorado news",
    "site:cbsnews.com/colorado Denver news",
    "site:cpr.org Colorado news",
]

# Brave News search queries (separate news endpoint for broader coverage)
NEWS_SEARCH_QUERIES = [
    "Denver Colorado news",
    "Denver metro news",
    "Colorado Front Range news",
]

# --- Weather Configuration ---
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_PARAMS = {
    "latitude": 39.74,
    "longitude": -104.98,
    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl",
    "daily": "temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,precipitation_sum,snowfall_sum,precipitation_probability_max,windspeed_10m_max,wind_gusts_10m_max,weathercode,sunrise,sunset,uv_index_max",
    "temperature_unit": "fahrenheit",
    "wind_speed_unit": "mph",
    "timezone": "America/Denver",
}

# Wind direction degrees to cardinal
WIND_DIRECTIONS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# Denver average highs by month (°F) for "above/below average" callout
DENVER_AVG_HIGHS = {
    1: 45, 2: 48, 3: 55, 4: 62, 5: 71, 6: 83,
    7: 90, 8: 88, 9: 80, 10: 66, 11: 53, 12: 45,
}

def wind_direction_label(degrees):
    """Convert wind degrees to cardinal direction (N, NE, etc.)."""
    if degrees is None:
        return ""
    idx = round(degrees / 22.5) % 16
    return WIND_DIRECTIONS[idx]

def uv_label(uv_val):
    """Return UV index category and advice."""
    if uv_val is None or uv_val < 0:
        return ("Low", "uv-low", "")
    if uv_val < 3:
        return ("Low", "uv-low", "No protection needed")
    if uv_val < 6:
        return ("Moderate", "uv-moderate", "Wear sunscreen after 10 AM")
    if uv_val < 8:
        return ("High", "uv-high", "Reduce sun exposure 10 AM - 4 PM")
    if uv_val < 11:
        return ("Very High", "uv-very-high", "Avoid midday sun, seek shade")
    return ("Extreme", "uv-extreme", "Stay indoors during midday hours")

# WMO Weather Code to human-readable conditions
WMO_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


# --- Weekend Events Configuration (Friday only) ---
WEEKEND_EVENT_QUERIES = [
    "Denver events this weekend",
    "things to do Denver this weekend",
    "Denver weekend activities",
    "Denver concerts shows this weekend",
    "Denver festivals markets this weekend",
    "Denver comedy shows this weekend",
    "Denver sports events this weekend",
    "Denver family events kids this weekend",
    "Denver art gallery museum opening this weekend",
    "site:westword.com Denver events this weekend",
    "site:303magazine.com Denver events this weekend",
    "site:denver.org things to do this weekend",
]

EVENTS_SYSTEM_PROMPT = """You are the events editor for 303 News, a Denver metro daily digest. You curate weekend event picks for Denver metro residents. Be factual and helpful. No editorializing, no emojis."""

# Preferred sources for article fetching (most reliable HTML)
PREFERRED_SOURCES = [
    "denverpost.com",
    "denvergazette.com",
    "coloradosun.com",
    "cpr.org",
    "bizjournals.com",
    "9news.com",
    "denver7.com",
    "kdvr.com",
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
    """Exit early if Denver local time is before 5:30 AM or if the wrong DST cron fired.

    Two crons fire daily: 11:45 UTC (for MDT) and 12:45 UTC (for MST).
    This guard ensures only the correct one produces output by checking
    whether the current UTC hour matches the active timezone offset.
    """
    now = datetime.datetime.now(DENVER_TZ)
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    is_dst = bool(now.dst())
    utc_hour = utc_now.hour

    # 11:45 UTC cron should only run during MDT (dst=True)
    # 12:45 UTC cron should only run during MST (dst=False)
    if utc_hour <= 11 and not is_dst:
        print(f"Denver time is {now.strftime('%H:%M %Z')} (MST) but UTC hour is {utc_hour} (MDT cron). Skipping.")
        sys.exit(0)
    if utc_hour >= 12 and is_dst:
        print(f"Denver time is {now.strftime('%H:%M %Z')} (MDT) but UTC hour is {utc_hour} (MST cron). Skipping.")
        sys.exit(0)

    if now.hour < 5 or (now.hour == 5 and now.minute < 30):
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


def extract_publish_date_from_url(url):
    """Extract publish date from URL path patterns like /2026/02/20/ or /2026-02-20."""
    m = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r'/(\d{4})-(\d{2})-(\d{2})/', url)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def filter_by_publish_date(candidates, target_date_str, max_delta_days=1):
    """Remove candidates whose URL-embedded publish date is too far from the target date."""
    target = datetime.date.fromisoformat(target_date_str)
    kept = []
    removed = 0
    for c in candidates:
        pub_date = extract_publish_date_from_url(c["url"])
        if pub_date is not None:
            delta = abs((pub_date - target).days)
            if delta > max_delta_days:
                removed += 1
                print(f"  Date filter: '{c['title'][:55]}...' (published {pub_date}, target {target_date_str})")
                continue
        kept.append(c)
    if removed:
        print(f"  Removed {removed} stories with wrong publish dates, {len(kept)} remain")
    return kept


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
        "coloradopolitics.com": "Colorado Politics",
        "si.com": "Sports Illustrated",
        "gazette.com": "Colorado Springs Gazette",
        "politico.com": "Politico",
        "westword.com": "Westword",
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


def brave_news_search(query, api_key, count=10, freshness="pd"):
    """Run a Brave News Search API query (news-specific endpoint). Returns list of result dicts."""
    global brave_query_count
    brave_query_count += 1
    if brave_query_count > MAX_BRAVE_QUERIES_PER_RUN:
        print(f"  LIMIT: Brave query cap ({MAX_BRAVE_QUERIES_PER_RUN}) reached. Skipping.")
        return []

    url = "https://api.search.brave.com/res/v1/news/search"
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
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
                "source": extract_source_name(item.get("url", "")),
            })
        return results
    except Exception as e:
        print(f"  News search failed for '{query}': {e}")
        return []


def gather_search_results(api_key, freshness="pd"):
    """Run all search queries (web + news) and collect results."""
    all_results = []

    # Web search queries
    for query in SEARCH_QUERIES:
        print(f"  Searching (web): {query}")
        results = brave_search(query, api_key, freshness=freshness)
        all_results.extend(results)
        time.sleep(0.3)  # be polite to the API

    # News search queries (separate endpoint, like Google News tab)
    for query in NEWS_SEARCH_QUERIES:
        print(f"  Searching (news): {query}")
        results = brave_news_search(query, api_key, freshness=freshness)
        all_results.extend(results)
        time.sleep(0.3)

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
            if len(shared) >= 4:
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


# Words in a candidate headline that suggest a genuinely new development
# on a previously covered story (arrest after a crime, verdict after trial, etc.)
UPDATE_INDICATOR_WORDS = {
    "update", "arrested", "arrest", "charged", "convicted", "verdict",
    "sentenced", "indicted", "identified", "confirmed", "dead", "dies",
    "killed", "death toll", "aftermath", "response", "fallout", "ruling",
    "decision", "settlement", "reopened", "recalled", "expanded", "closed",
    "reopens", "closes", "cleared", "suspended", "fired", "resigned",
    "released", "evacuated", "contained", "extinguished", "reopened",
    "investigation", "cause", "lawsuit", "sues", "sued",
}


def has_update_indicators(title_lower):
    """Check if a headline contains words suggesting a new development on a known story."""
    title_words = set(re.findall(r'[a-z]+', title_lower))
    return bool(title_words & UPDATE_INDICATOR_WORDS)


def filter_cross_day_duplicates(candidates, target_date_str):
    """Remove candidates whose headlines are too similar to recent days' stories.
    Allows potential updates (new developments) through for Claude to evaluate."""
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
                # Very high similarity -- only allow through if update indicators present
                if has_update_indicators(title_lower):
                    print(f"  Potential update allowed: '{candidate['title'][:60]}...'")
                    break
                is_duplicate = True
                print(f"  Cross-day dup removed: '{candidate['title'][:60]}...'")
                break
            # Also check keyword overlap (raised threshold from 3 to 4)
            candidate_words = extract_significant_words(title_lower)
            prev_words = extract_significant_words(prev_headline)
            shared = candidate_words & prev_words
            if len(shared) >= 4:
                if has_update_indicators(title_lower):
                    print(f"  Potential update allowed (keywords): '{candidate['title'][:60]}...'")
                    break
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


def get_daily_joke(date_str):
    """Pick today's joke from the joke bank based on date rotation."""
    joke_bank_path = os.path.join(os.path.dirname(__file__), "joke_bank.json")
    try:
        with open(joke_bank_path, "r", encoding="utf-8") as f:
            jokes = json.load(f)
    except Exception as e:
        print(f"  Error loading joke bank: {e}")
        return None

    if not jokes:
        print("  Joke bank is empty")
        return None

    target_date = datetime.date.fromisoformat(date_str)
    day_index = target_date.timetuple().tm_yday + target_date.year * 366
    joke = jokes[day_index % len(jokes)]
    return {"comedian": joke["comedian"], "joke": joke["joke"]}


# --- Top World Story ---

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss"

# Sources to prefer for world news article fetching
WORLD_NEWS_PREFERRED = [
    "apnews.com", "reuters.com", "bbc.com", "bbc.co.uk",
    "nytimes.com", "washingtonpost.com", "theguardian.com",
    "cbsnews.com", "nbcnews.com", "abcnews.go.com",
    "aljazeera.com", "npr.org", "politico.com", "bloomberg.com",
]

# Sources to deprioritize (entertainment, clickbait)
WORLD_NEWS_NOISE = [
    "tmz.com", "eonline.com", "people.com", "usmagazine.com",
    "buzzfeed.com", "dailymail.co.uk", "pagesix.com",
    "gizmodo.com", "lifehacker.com", "kotaku.com",
]

TOP_STORY_SYSTEM_PROMPT = """You are the international desk editor for 303 News, a Denver metro daily digest. You write detailed, factual summaries of major world and national news stories. Clean newspaper style. No editorializing, no emojis."""


def fetch_google_news_rss():
    """Fetch and parse Google News RSS feed. Returns list of {title, link, source, pub_date}."""
    print("  Fetching Google News RSS...")
    try:
        result = subprocess.run(
            ["curl", "-sL", GOOGLE_NEWS_RSS_URL,
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             "--max-time", "15"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print("  Google News RSS fetch failed (curl error)")
            return []

        root = ET.fromstring(result.stdout)
        items = root.findall(".//item")
        print(f"  Google News RSS: {len(items)} items")

        entries = []
        for item in items[:20]:  # top 20 is plenty
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            if title_el is None or link_el is None:
                continue

            raw_title = title_el.text or ""
            # Google News format: "Headline text - Source Name"
            if " - " in raw_title:
                parts = raw_title.rsplit(" - ", 1)
                headline = parts[0].strip()
                source = parts[1].strip()
            else:
                headline = raw_title.strip()
                source = "Unknown"

            entries.append({
                "title": headline,
                "link": link_el.text or "",
                "source": source,
                "pub_date": pub_el.text if pub_el is not None else "",
            })

        return entries

    except Exception as e:
        print(f"  Google News RSS failed: {e}")
        return []


def resolve_google_news_url(google_url):
    """Resolve a Google News redirect URL to the actual article URL."""
    if "news.google.com" not in google_url:
        return google_url
    try:
        result = subprocess.run(
            ["curl", "-sI", "-L", google_url,
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             "--max-time", "10", "--max-redirs", "5"],
            capture_output=True, text=True, timeout=15,
        )
        # Look for the final Location header
        final_url = google_url
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.lower().startswith("location:"):
                candidate = line.split(":", 1)[1].strip()
                if "news.google.com" not in candidate:
                    final_url = candidate
        return final_url
    except Exception:
        return google_url


def cluster_headlines(entries):
    """Group RSS entries by topic similarity. Returns list of clusters (each a list of entries)."""
    clusters = []
    used = set()

    for i, entry in enumerate(entries):
        if i in used:
            continue
        cluster = [entry]
        used.add(i)
        for j, other in enumerate(entries):
            if j in used:
                continue
            # Check headline similarity
            sim = SequenceMatcher(
                None, entry["title"].lower(), other["title"].lower()
            ).ratio()
            if sim > 0.35:
                cluster.append(other)
                used.add(j)
                continue
            # Check keyword overlap
            words_a = extract_significant_words(entry["title"])
            words_b = extract_significant_words(other["title"])
            shared = words_a & words_b
            if len(shared) >= 3:
                cluster.append(other)
                used.add(j)
        clusters.append(cluster)

    return clusters


def score_cluster(cluster):
    """Score a cluster by source diversity and news quality."""
    sources = set(e["source"] for e in cluster)
    # Bonus for preferred news sources
    preferred_count = sum(
        1 for s in sources
        if any(p in s.lower() for p in ["ap ", "reuters", "bbc", "nyt", "washington post",
                                         "cbs", "nbc", "abc", "guardian", "npr", "bloomberg"])
    )
    # Penalty for noise sources
    noise_count = sum(
        1 for e in cluster
        if any(n in e.get("link", "").lower() for n in WORLD_NEWS_NOISE)
    )
    return len(sources) * 2 + preferred_count - noise_count * 3


def fetch_top_world_story(brave_key, target_date_str):
    """Identify and summarize the top world news story using Google News RSS + Brave verification."""
    global anthropic_call_count, brave_query_count

    # Step 1: Get Google News RSS entries
    entries = fetch_google_news_rss()
    if not entries:
        print("  No Google News entries, falling back to Brave-only")
        entries = []

    # Step 2: Cluster by topic
    clusters = cluster_headlines(entries)
    if clusters:
        # Score and sort
        scored = [(score_cluster(c), c) for c in clusters]
        scored.sort(key=lambda x: x[0], reverse=True)
        top_cluster = scored[0][1]
        print(f"  Top cluster ({len(top_cluster)} articles, score {scored[0][0]}):")
        for e in top_cluster[:5]:
            print(f"    - [{e['source']}] {e['title'][:80]}")
    else:
        top_cluster = []

    # Step 3: Verify/supplement with Brave News search
    if top_cluster:
        # Extract key terms from the top cluster for verification
        all_words = set()
        for e in top_cluster[:3]:
            all_words |= extract_significant_words(e["title"])
        # Pick the 3-4 most common significant words
        word_freq = {}
        for e in top_cluster:
            for w in extract_significant_words(e["title"]):
                word_freq[w] = word_freq.get(w, 0) + 1
        top_terms = sorted(word_freq, key=word_freq.get, reverse=True)[:4]
        verify_query = " ".join(top_terms)
    else:
        verify_query = "top news today breaking"

    print(f"  Brave verification query: '{verify_query}'")
    brave_results = []
    if brave_query_count < MAX_BRAVE_QUERIES_PER_RUN:
        brave_results = brave_news_search(verify_query, brave_key, count=10, freshness="pd")
        time.sleep(0.3)

    # Also try a broader query if we have budget
    if brave_query_count < MAX_BRAVE_QUERIES_PER_RUN:
        broad = brave_news_search("top world news today", brave_key, count=10, freshness="pd")
        brave_results.extend(broad)
        time.sleep(0.3)

    # Merge Brave results into our entry pool
    for br in brave_results:
        # Skip if it's noise
        if any(n in br["url"] for n in WORLD_NEWS_NOISE):
            continue
        entries.append({
            "title": br["title"],
            "link": br["url"],
            "source": br["source"],
            "pub_date": "",
        })

    # Re-cluster with the combined pool
    if brave_results:
        all_clusters = cluster_headlines(entries)
        scored = [(score_cluster(c), c) for c in all_clusters]
        scored.sort(key=lambda x: x[0], reverse=True)
        top_cluster = scored[0][1]
        print(f"  Final top cluster ({len(top_cluster)} articles, score {scored[0][0]}):")
        for e in top_cluster[:5]:
            print(f"    - [{e['source']}] {e['title'][:80]}")

    if not top_cluster:
        print("  Could not identify a top world story")
        return None

    # Step 4: Fetch full article text from the best sources in the cluster
    # Prefer major wire services and newspapers
    sorted_entries = sorted(
        top_cluster,
        key=lambda e: (
            1 if any(p in e.get("link", "").lower() for p in WORLD_NEWS_PREFERRED) else 0
        ),
        reverse=True,
    )

    article_texts = []
    sources_used = []
    for entry in sorted_entries[:5]:  # try up to 5
        url = entry["link"]
        # Resolve Google News redirect URLs
        if "news.google.com" in url:
            url = resolve_google_news_url(url)
            if "news.google.com" in url:
                continue  # couldn't resolve
        text = fetch_article_text(url)
        if text and len(text) > 200:
            article_texts.append({
                "source": entry["source"],
                "url": url,
                "text": text,
            })
            sources_used.append({"name": entry["source"], "url": url})
            print(f"    Fetched {len(text)} chars from {entry['source']}")
            if len(article_texts) >= 3:
                break
        time.sleep(0.3)

    if not article_texts:
        print("  Could not fetch article text for top story")
        return None

    # Step 5: Send to Claude for detailed summary
    anthropic_call_count += 1
    if anthropic_call_count > MAX_ANTHROPIC_CALLS_PER_RUN:
        print(f"  LIMIT: Anthropic call cap reached. Skipping top story.")
        return None

    source_blocks = []
    for i, at in enumerate(article_texts, 1):
        source_blocks.append(f"[SOURCE {i}: {at['source']}]\n{at['text']}")
    sources_text = "\n\n".join(source_blocks)

    # Build the representative headline from the cluster
    representative_headline = top_cluster[0]["title"]

    user_prompt = f"""Below are {len(article_texts)} articles about the top news story of the day.

Topic: {representative_headline}

{sources_text}

---

Write a detailed summary of this story. Your summary must be at least 3 paragraphs long (4-5 paragraphs preferred), with each paragraph 2-4 sentences. Cover:
- What happened (the core facts)
- Who is involved and what they said or did
- The broader context and background
- Why it matters and what comes next

Also write a clear, factual headline for the story.

Focus on hard news with genuine global or national significance. Write in clean newspaper style. No editorializing, no emojis.

Return ONLY a JSON object with these fields:
- "headline": clear factual headline
- "summary": the full multi-paragraph summary with \\n\\n between paragraphs

Return ONLY the JSON object, no other text."""

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=TOP_STORY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text
        usage = response.usage
        cost = (usage.input_tokens * 3 + usage.output_tokens * 15) / 1_000_000
        print(f"  Top story summary: {usage.input_tokens + usage.output_tokens} tokens, ${cost:.4f}")

        # Parse JSON
        result = None
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            pass
        if not result:
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_text, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
        if not result:
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

        if not result or "headline" not in result or "summary" not in result:
            print(f"  Failed to parse top story JSON")
            return None

        # Attach source list
        result["sources"] = sources_used
        print(f"  Top story: {result['headline'][:70]}")
        return result

    except Exception as e:
        print(f"  Top story summarization failed: {e}")
        return None


def fetch_weather_forecast(target_date_str):
    """Fetch Denver weather forecast from Open-Meteo with current conditions and extended detail."""
    print("  Fetching Open-Meteo forecast...")
    try:
        resp = requests.get(OPEN_METEO_URL, params=OPEN_METEO_PARAMS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  Weather API failed: {e}")
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    try:
        idx = dates.index(target_date_str)
    except ValueError:
        print(f"  Target date {target_date_str} not found in forecast data")
        return None

    high = daily["temperature_2m_max"][idx]
    low = daily["temperature_2m_min"][idx]
    precip_mm = daily.get("precipitation_sum", [0])[idx] or 0
    snowfall_cm = daily.get("snowfall_sum", [0])[idx] or 0
    precip_prob = daily.get("precipitation_probability_max", [0])[idx] or 0
    wind_mph = daily.get("windspeed_10m_max", [0])[idx] or 0
    wind_gusts = daily.get("wind_gusts_10m_max", [0])[idx] or 0
    code = daily.get("weathercode", [0])[idx]
    conditions = WMO_WEATHER_CODES.get(code, "Unknown")
    feels_high = daily.get("apparent_temperature_max", [None])[idx]
    feels_low = daily.get("apparent_temperature_min", [None])[idx]
    sunrise_raw = daily.get("sunrise", [None])[idx]
    sunset_raw = daily.get("sunset", [None])[idx]
    uv_max = daily.get("uv_index_max", [None])[idx]

    # Parse sunrise/sunset times
    sunrise_str = ""
    sunset_str = ""
    daylight_str = ""
    if sunrise_raw and sunset_raw:
        try:
            sr = datetime.datetime.fromisoformat(sunrise_raw)
            ss = datetime.datetime.fromisoformat(sunset_raw)
            sunrise_str = sr.strftime("%-I:%M %p")
            sunset_str = ss.strftime("%-I:%M %p")
            dl = ss - sr
            hours = int(dl.total_seconds() // 3600)
            minutes = int((dl.total_seconds() % 3600) // 60)
            daylight_str = f"{hours}h {minutes}m"
        except Exception:
            pass

    # UV label
    uv_cat, uv_class, uv_advice = uv_label(uv_max)

    # Current conditions (real-time snapshot)
    current = data.get("current", {})
    current_temp = current.get("temperature_2m")
    current_feels = current.get("apparent_temperature")
    current_humidity = current.get("relative_humidity_2m")
    current_wind = current.get("wind_speed_10m")
    current_wind_dir = current.get("wind_direction_10m")
    current_gusts = current.get("wind_gusts_10m")
    current_pressure_hpa = current.get("pressure_msl")
    current_cloud = current.get("cloud_cover")
    current_code = current.get("weather_code")
    current_conditions = WMO_WEATHER_CODES.get(current_code, conditions) if current_code is not None else conditions
    current_time_raw = current.get("time", "")

    # Convert pressure from hPa to inHg
    current_pressure_inhg = round(current_pressure_hpa * 0.02953, 2) if current_pressure_hpa else None

    # Current time label (e.g. "7:00 AM")
    current_time_label = ""
    if current_time_raw:
        try:
            ct = datetime.datetime.fromisoformat(current_time_raw)
            current_time_label = ct.strftime("%-I:%M %p")
        except Exception:
            pass

    # Wind direction for current
    current_wind_dir_label = wind_direction_label(current_wind_dir)

    # "Above/below average" callout
    notable = ""
    target_month = int(target_date_str.split("-")[1])
    avg_high = DENVER_AVG_HIGHS.get(target_month, 0)
    if avg_high and high:
        diff = round(high - avg_high)
        if diff >= 10:
            notable = f"About {diff} degrees above the average high for this time of year!"
        elif diff >= 5:
            notable = f"Nearly {diff} degrees above the average high for this time of year."
        elif diff <= -10:
            notable = f"About {abs(diff)} degrees below the average high for this time of year."
        elif diff <= -5:
            notable = f"Nearly {abs(diff)} degrees below the average high for this time of year."

    weather = {
        "high": round(high),
        "low": round(low),
        "conditions": conditions,
        "wind_mph": round(wind_mph, 1) if wind_mph else 0,
        "wind_gusts_mph": round(wind_gusts, 1) if wind_gusts else 0,
        "precipitation_inches": round(precip_mm / 25.4, 2) if precip_mm else 0,
        "snowfall_inches": round(snowfall_cm / 2.54, 1) if snowfall_cm else 0,
        "precipitation_probability": round(precip_prob),
        "feels_high": round(feels_high) if feels_high is not None else None,
        "feels_low": round(feels_low) if feels_low is not None else None,
        "sunrise": sunrise_str,
        "sunset": sunset_str,
        "daylight": daylight_str,
        "uv_index": round(uv_max, 1) if uv_max is not None else None,
        "uv_label": uv_cat,
        "uv_class": uv_class,
        "uv_advice": uv_advice,
        "notable": notable,
        "current": {
            "temp": round(current_temp) if current_temp is not None else None,
            "feels_like": round(current_feels) if current_feels is not None else None,
            "humidity": round(current_humidity) if current_humidity is not None else None,
            "conditions": current_conditions,
            "wind_mph": round(current_wind, 1) if current_wind is not None else None,
            "wind_dir": current_wind_dir_label,
            "wind_gusts_mph": round(current_gusts, 1) if current_gusts is not None else None,
            "pressure_inhg": current_pressure_inhg,
            "cloud_cover": round(current_cloud) if current_cloud is not None else None,
            "time_label": current_time_label,
        },
    }

    print(f"  Weather: {conditions}, High {weather['high']}F, Low {weather['low']}F, "
          f"Precip prob {weather['precipitation_probability']}%, "
          f"Snow {weather['snowfall_inches']}in, UV {uv_max}")

    return weather




def fetch_weekend_events(brave_key, target_date_str, freshness):
    """Search Brave for Denver weekend events and curate via Claude. Friday only."""
    target_date = datetime.date.fromisoformat(target_date_str)
    if target_date.weekday() != 4:  # 4 = Friday
        return None

    print("  Searching for Denver weekend events...")
    all_results = []
    # Use past-week freshness for events (listings published days in advance)
    events_freshness = "pw"
    for query in WEEKEND_EVENT_QUERIES:
        print(f"    Searching: {query}")
        results = brave_search(query, brave_key, count=10, freshness=events_freshness)
        all_results.extend(results)
        time.sleep(0.3)

    if not all_results:
        print("  No weekend event results found")
        return []

    # Deduplicate by URL
    seen_urls = set()
    unique = []
    for r in all_results:
        norm = r["url"].split("?")[0].rstrip("/")
        if norm not in seen_urls:
            seen_urls.add(norm)
            unique.append(r)

    print(f"  {len(unique)} unique event results")

    # Fetch article text for top results (limit to 20 for broader coverage)
    fetch_limit = min(20, len(unique))
    for r in unique[:fetch_limit]:
        text = fetch_article_text(r["url"])
        if text:
            r["article_text"] = text
        else:
            r["article_text"] = r.get("snippet", "")

    # Send to Claude for curation
    events = curate_weekend_events(unique[:fetch_limit], target_date_str)

    # Post-processing: validate event dates fall within Fri-Sun window
    if events:
        events = validate_event_dates(events, target_date)

    return events


def curate_weekend_events(results, target_date_str):
    """Send event search results to Claude for curation into structured event list."""
    global anthropic_call_count
    anthropic_call_count += 1
    if anthropic_call_count > MAX_ANTHROPIC_CALLS_PER_RUN:
        print(f"  LIMIT: Anthropic call cap reached. Skipping events curation.")
        return []

    target = datetime.date.fromisoformat(target_date_str)
    fri = target
    sat = target + datetime.timedelta(days=1)
    sun = target + datetime.timedelta(days=2)
    fri_str = fri.strftime("%A, %B %d").replace(" 0", " ")
    sat_str = sat.strftime("%A, %B %d").replace(" 0", " ")
    sun_str = sun.strftime("%A, %B %d").replace(" 0", " ")

    event_blocks = []
    for i, r in enumerate(results, 1):
        block = f"""[SOURCE {i}]
Title: {r['title']}
URL: {r['url']}
Text: {r.get('article_text', r.get('snippet', ''))}"""
        event_blocks.append(block)

    events_text = "\n\n".join(event_blocks)

    user_prompt = f"""This weekend's dates:
- {fri_str} (Friday)
- {sat_str} (Saturday)
- {sun_str} (Sunday)

IMPORTANT: ONLY include events happening on one of these three specific dates above. Do NOT include events from other weekends, future months, or recurring listings without a confirmed date this weekend.

Below are {len(results)} search results about Denver weekend events and activities. Extract and curate the best 8-12 specific events happening THIS weekend in the Denver metro area. Aim for variety: mix concerts, comedy, sports, family-friendly, outdoor, food/drink, and cultural events.

{events_text}

---

For each event, produce a JSON object with:
- "title": event name
- "description": 1-2 sentences about what the event is
- "date": the specific date(s) using format like "{fri_str}" or "{sat_str}-{sun_str}"
- "time": time if known, otherwise "See venue for times"
- "location": venue and city
- "url": link to event info or ticket page

Return ONLY a JSON array. If you cannot find at least 5 specific events with real details for this weekend, return an empty array []."""

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=3000,
            system=EVENTS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text
        usage = response.usage
        cost = (usage.input_tokens * 3 + usage.output_tokens * 15) / 1_000_000
        print(f"  Events curation: {usage.input_tokens + usage.output_tokens} tokens, ${cost:.4f}")

        # Parse JSON (same pattern as call_anthropic)
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            pass
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        match = re.search(r"\[.*\]", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        print(f"  Failed to parse events JSON")
        return []
    except Exception as e:
        print(f"  Events curation failed: {e}")
        return []


def validate_event_dates(events, target_friday):
    """Remove events whose dates don't fall within the Fri-Sun weekend window."""
    sat = target_friday + datetime.timedelta(days=1)
    sun = target_friday + datetime.timedelta(days=2)

    valid_months = set()
    for d in [target_friday, sat, sun]:
        valid_months.add(d.strftime("%B").lower())
        valid_months.add(d.strftime("%b").lower())

    valid_days = set()
    for d in [target_friday, sat, sun]:
        valid_days.add(str(d.day))

    valid_day_names = {"friday", "saturday", "sunday"}

    kept = []
    for evt in events:
        date_str = evt.get("date", "").lower()

        # Check if the event date mentions valid day names or day numbers
        has_valid_day_name = any(name in date_str for name in valid_day_names)
        has_valid_day_number = any(
            f" {d}" in f" {date_str}" or date_str.endswith(d)
            for d in valid_days
        )
        has_valid_month = any(m in date_str for m in valid_months)

        if has_valid_day_name or (has_valid_month and has_valid_day_number):
            kept.append(evt)
        else:
            print(f"  Event date validation: removed '{evt.get('title', '')[:50]}' (date: '{evt.get('date', '')}')")

    if len(kept) < len(events):
        print(f"  Validated events: {len(kept)} of {len(events)} passed date check")

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

    # Load previous days' headlines so Claude can avoid repeats
    prev_headlines = load_recent_headlines(target_date_str)
    dedup_block = ""
    if prev_headlines:
        hl_list = "\n".join(f"- {h}" for h in prev_headlines[:36])
        dedup_block = f"""
IMPORTANT -- STORIES ALREADY PUBLISHED ON PREVIOUS DAYS (do NOT repeat these):
{hl_list}

Do NOT select any candidate that covers the same event as the headlines above. Only include a story if there is a genuinely new development not covered by the previous headline."""

    num_to_produce = min(MAX_ARTICLES, len(stories))

    return f"""Date: {target_date_str}. Below are {len(stories)} candidate Denver metro news stories.

YOUR TASK: Write summaries for as many stories as possible, up to {MAX_ARTICLES}. Include ALL candidates that have genuine Denver/Colorado relevance. Editorial guidelines:
- Include every story that is relevant to Denver metro residents, even if the impact is moderate or the story is smaller in scope
- It is better to include a less impactful story than to leave the digest thin -- aim for {MAX_ARTICLES} stories
- If you have fewer than {MAX_ARTICLES} candidates, include ALL of them (do not drop any)
- Ensure category balance when possible: aim for at least 1-2 stories per category (other, politics, business, crime, sports)
- HARD LIMIT: No more than 3 stories in any single category, especially sports. If you have more than 3 candidates in one category, pick only the 3 most newsworthy
- Prefer stories from credible local sources (Denver Post, Colorado Sun, Denver Gazette, CPR News, Denver Business Journal, 9News, Denver7)
- Only drop a story if it is truly clickbait, entirely national with zero Denver relevance, or an exact duplicate of another candidate
- If two candidates cover the same event, pick the one with better sourcing
- CRITICAL: Do NOT include stories that were already covered on previous days (see list below)
- EXCEPTION: If a previously covered story has a genuinely new development (arrest, verdict, policy change, new damage estimate, etc.), you may include it BUT you MUST prefix the headline with "Update: " (e.g., "Update: Suspect in Denver shooting arraigned on first-degree murder charge")
{dedup_block}

{stories_text}

---

Produce a JSON array of {num_to_produce} stories (or as many as are genuinely relevant, up to {MAX_ARTICLES}). For each story:
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


def write_output(stories_json, date_str, date_formatted, joke=None, weather=None, weekend_events=None, top_story=None):
    """Write the final JSON data file."""
    output = {
        "date": date_str,
        "dateFormatted": date_formatted,
        "stories": stories_json,
    }
    if weather:
        output["weather"] = weather
    if joke:
        output["joke"] = joke
    if weekend_events:
        output["weekendEvents"] = weekend_events
    if top_story:
        output["topStory"] = top_story

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
    else:
        # Use date-range freshness for backfill: target_date +/- 1 day
        start = (target_date - datetime.timedelta(days=1)).isoformat()
        end = (target_date + datetime.timedelta(days=1)).isoformat()
        return f"{start}to{end}"


def build_email_html(stories_json, date_str, date_formatted, joke=None, weather=None, weekend_events=None, top_story=None):
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

    # Build weather HTML (appears after header, before news)
    weather_html = ""
    if weather and weather.get("conditions"):
        precip_line = ""
        snow_in = weather.get("snowfall_inches", 0)
        rain_in = weather.get("precipitation_inches", 0)
        if snow_in > 0:
            precip_line = f'<div style="font-family: Georgia, \'Times New Roman\', serif; font-size: 14px; color: #333; margin-top: 4px;">Snow: ~{snow_in:.1f}&quot; expected</div>'
        elif rain_in > 0:
            precip_line = f'<div style="font-family: Georgia, \'Times New Roman\', serif; font-size: 14px; color: #333; margin-top: 4px;">Rain: ~{rain_in:.2f}&quot; expected</div>'
        precip_prob = weather.get("precipitation_probability", 0)
        prob_line = ""
        if precip_prob > 0:
            prob_line = f'<div style="font-family: Georgia, \'Times New Roman\', serif; font-size: 13px; color: #666; margin-top: 4px;">{precip_prob}% chance of precipitation</div>'

        # Current conditions block
        cur = weather.get("current", {})
        current_html = ""
        if cur.get("temp") is not None:
            feels_str = f' (feels like {cur["feels_like"]}&deg;F)' if cur.get("feels_like") is not None else ""
            humid_str = f' &bull; Humidity {cur["humidity"]}%' if cur.get("humidity") is not None else ""
            press_str = f' &bull; Pressure {cur["pressure_inhg"]} inHg' if cur.get("pressure_inhg") else ""
            time_label = f' (as of {cur["time_label"]})' if cur.get("time_label") else ""
            current_html = f'''
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; color: #999; text-transform: uppercase; margin-bottom: 4px;">Current Conditions{time_label}</div>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 18px; color: #222;"><strong>{cur["temp"]}&deg;F</strong>{feels_str}</div>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 13px; color: #555; margin-bottom: 12px;">{cur.get("conditions", "")}{humid_str}{press_str}</div>
        <div style="border-top: 1px dotted #ccc; margin-bottom: 10px;"></div>
'''

        # Extended detail lines
        feels_line = ""
        if weather.get("feels_high") is not None and weather.get("feels_low") is not None:
            feels_line = f'<div style="font-family: Georgia, \'Times New Roman\', serif; font-size: 13px; color: #666; margin-top: 2px;">Feels like {weather["feels_high"]}&deg; / {weather["feels_low"]}&deg;</div>'
        gusts_line = ""
        if weather.get("wind_gusts_mph") and weather["wind_gusts_mph"] > 0:
            gusts_line = f', gusts {weather["wind_gusts_mph"]} mph'
        sun_line = ""
        if weather.get("sunrise") and weather.get("sunset"):
            dl = f' ({weather["daylight"]})' if weather.get("daylight") else ""
            sun_line = f'<div style="font-family: Georgia, \'Times New Roman\', serif; font-size: 14px; color: #333; margin-top: 4px;">Sunrise {weather["sunrise"]} / Sunset {weather["sunset"]}{dl}</div>'
        uv_line = ""
        if weather.get("uv_index") is not None:
            uv_line = f'<div style="font-family: Georgia, \'Times New Roman\', serif; font-size: 14px; color: #333; margin-top: 4px;">UV Index: {weather["uv_index"]} ({weather["uv_label"]})</div>'
        notable_line = ""
        if weather.get("notable"):
            notable_line = f'''
        <div style="margin-top: 10px; padding: 8px 12px; background-color: #fdf6e3; border-left: 3px solid #c9a959; font-family: Georgia, 'Times New Roman', serif; font-size: 13px; font-style: italic; color: #665;">{weather["notable"]}</div>'''

        weather_html = f'''
    <tr><td style="padding: 20px 16px; background-color: #eee8d5; border-left: 3px solid #c9a959;">
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 11px; font-weight: 700; letter-spacing: 2px; color: #888; text-transform: uppercase; margin-bottom: 8px;">TODAY'S FORECAST</div>
        {current_html}
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 16px; color: #333; font-weight: 600;">{weather["conditions"]}</div>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 15px; color: #333; margin-top: 4px;"><strong>High {weather["high"]}&deg;F</strong> / Low {weather["low"]}&deg;F</div>
        {feels_line}
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 14px; color: #333; margin-top: 4px;">Wind: {weather.get("wind_mph", 0)} mph{gusts_line}</div>
        {precip_line}
        {prob_line}
        {sun_line}
        {uv_line}
        {notable_line}
    </td></tr>
'''

    # Build top world story HTML (appears after weather, before Denver news)
    top_story_html = ""
    if top_story and top_story.get("headline") and top_story.get("summary"):
        ts_headline = top_story["headline"]
        ts_summary = top_story["summary"]
        # Format sources as byline
        ts_sources = top_story.get("sources", [])
        ts_byline = " | ".join(s["name"] for s in ts_sources) if ts_sources else ""
        # Build paragraphs
        ts_paragraphs = ""
        for para in ts_summary.split("\n\n"):
            para = para.strip()
            if para:
                ts_paragraphs += f'<p style="font-family: Georgia, \'Times New Roman\', serif; font-size: 15px; color: #333; line-height: 1.6; margin: 0 0 12px 0;">{para}</p>\n'
        # Source links
        ts_source_links = ""
        for s in ts_sources:
            ts_source_links += f' <a href="{s["url"]}" style="font-family: Georgia, \'Times New Roman\', serif; font-size: 12px; color: #1a3a6a; text-decoration: none;">{s["name"]}</a> &middot;'
        if ts_source_links.endswith("&middot;"):
            ts_source_links = ts_source_links[:-8]

        top_story_html = f'''
    <tr><td style="padding: 20px 0 8px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 14px; font-weight: 700; letter-spacing: 2px; color: #444; text-transform: uppercase; border-bottom: 1px solid #ddd; border-top: 2px solid #c9a959;">TOP STORY IN THE WORLD</td></tr>
    <tr><td style="padding: 14px 0 4px 0;">
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 22px; color: #1a1a1a; line-height: 1.3; font-weight: 700;">{ts_headline}</div>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; color: #888; text-transform: uppercase; margin-top: 6px;">{ts_byline}</div>
    </td></tr>
    <tr><td style="padding: 10px 0 16px 0;">
        {ts_paragraphs}
        <div style="margin-top: 8px; font-size: 12px; color: #888;">Sources:{ts_source_links}</div>
    </td></tr>
'''

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

    # Build weekend events HTML (appears after news, before comic, Friday only)
    events_html = ""
    if weekend_events:
        events_rows = ""
        for evt in weekend_events:
            meta_parts = []
            if evt.get("date"):
                meta_parts.append(evt["date"])
            if evt.get("time"):
                meta_parts.append(evt["time"])
            if evt.get("location"):
                meta_parts.append(evt["location"])
            meta_str = " &middot; ".join(meta_parts)

            events_rows += f'''
        <tr><td style="padding: 10px 0 2px 0;">
            <a href="{evt.get('url', '#')}" style="font-family: Georgia, 'Times New Roman', serif; font-size: 16px; color: #1a3a6a; text-decoration: none;">{evt.get('title', '')}</a>
        </td></tr>
        <tr><td style="padding: 0 0 8px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 14px; color: #555; line-height: 1.5;">
            {evt.get('description', '')} <span style="font-size: 12px; color: #888; font-style: italic;">{meta_str}</span>
        </td></tr>
'''
        events_html = f'''
    <tr><td style="padding: 20px 0 8px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 14px; font-weight: 700; letter-spacing: 2px; color: #444; text-transform: uppercase; border-bottom: 1px solid #ddd; border-top: 2px solid #c9a959;">YOUR WEEKEND ACTIVITY GUIDE</td></tr>
{events_rows}
'''

    html = f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; background-color: #f5f0e8;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f5f0e8;">
<tr><td align="center" style="padding: 12px 8px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width: 720px;">
    <tr><td style="text-align: center; padding: 24px 0 20px 0; border-bottom: 3px double #1a1a1a;">
        <a href="{SITE_URL}" style="text-decoration: none;"><div style="font-family: Georgia, 'Times New Roman', serif; font-size: 32px; font-weight: 700; letter-spacing: 3px;"><span style="color: #8b1a1a;">303</span> <span style="color: #1a1a1a;">NEWS</span></div></a>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 14px; font-style: italic; color: #666; margin-top: 4px;">{date_formatted}</div>
    </td></tr>
{weather_html}
{top_story_html}
{events_html}
{sections_html}
    <tr><td style="padding: 24px 0 10px 0; text-align: center; border-top: 1px solid #ddd;">
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 11px; font-weight: 700; letter-spacing: 2px; color: #888; text-transform: uppercase;">Daily Laugh</div>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 15px; color: #333; margin-top: 6px; line-height: 1.5;">{joke['joke'] if joke else ''}</div>
        <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 12px; color: #999; font-style: italic; margin-top: 6px;">&mdash; {joke['comedian'] if joke else ''}</div>
    </td></tr>
    <tr><td style="padding: 20px 0 16px 0; text-align: center; border-top: 1px solid #ccc;">
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


def send_email(stories_json, date_str, date_formatted, joke=None, weather=None, weekend_events=None, top_story=None):
    """Send the daily digest email via Buttondown API."""
    buttondown_key = os.environ.get("BUTTONDOWN_API_KEY")
    if not buttondown_key:
        print("  BUTTONDOWN_API_KEY not set. Skipping email.")
        return

    email_html = build_email_html(stories_json, date_str, date_formatted,
                                  joke=joke, weather=weather, weekend_events=weekend_events,
                                  top_story=top_story)
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

    # Step 2b: Filter out articles with wrong publish dates
    print("\n[Step 2b] Filtering by publish date...")
    top_stories = filter_by_publish_date(top_stories, target_date_str)

    # Step 2c: Remove stories that appeared in previous days
    print("\n[Step 2c] Filtering cross-day duplicates...")
    top_stories = filter_cross_day_duplicates(top_stories, target_date_str)

    # Step 2d: Fallback -- if too few candidates survived, run supplemental searches
    MIN_CANDIDATES = 8
    if len(top_stories) < MIN_CANDIDATES:
        print(f"\n[Step 2d] Only {len(top_stories)} candidates survived filtering (minimum {MIN_CANDIDATES}). Running fallback searches...")
        fallback_queries = [
            "Denver news",
            "Colorado breaking news",
            "Denver events happening",
            "Aurora Colorado news",
            "Colorado Springs news today",
            "Boulder Colorado news",
            "Jefferson County Colorado news",
            "Adams County Colorado news",
        ]
        fallback_results = []
        for query in fallback_queries:
            if brave_query_count >= MAX_BRAVE_QUERIES_PER_RUN:
                break
            print(f"  Fallback search: {query}")
            results = brave_search(query, brave_key, freshness=freshness)
            fallback_results.extend(results)
            time.sleep(0.3)

        # Also try news endpoint with broader queries
        fallback_news_queries = [
            "Colorado news",
            "Denver area news",
        ]
        for query in fallback_news_queries:
            if brave_query_count >= MAX_BRAVE_QUERIES_PER_RUN:
                break
            print(f"  Fallback news search: {query}")
            results = brave_news_search(query, brave_key, freshness=freshness)
            fallback_results.extend(results)
            time.sleep(0.3)

        if fallback_results:
            print(f"  Fallback returned {len(fallback_results)} raw results")
            fallback_deduped = deduplicate_and_rank(fallback_results)
            fallback_deduped = filter_by_publish_date(fallback_deduped, target_date_str)
            fallback_deduped = filter_cross_day_duplicates(fallback_deduped, target_date_str)

            # Add new candidates that aren't already in top_stories
            existing_urls = set(s["url"].split("?")[0].rstrip("/") for s in top_stories)
            added = 0
            for story in fallback_deduped:
                norm_url = story["url"].split("?")[0].rstrip("/")
                if norm_url not in existing_urls:
                    top_stories.append(story)
                    existing_urls.add(norm_url)
                    added += 1
            print(f"  Added {added} new candidates from fallback searches")
            print(f"  Total candidates now: {len(top_stories)}")

    if not top_stories:
        print("ERROR: No stories after all filtering")
        sys.exit(1)

    # Step 3: Fetch article content
    print("\n[Step 3] Fetching article content...")
    stories_with_text = fetch_articles(top_stories)

    # Step 4: Curate and summarize via Anthropic
    print(f"\n[Step 4] Sending {len(stories_with_text)} candidates to Anthropic for curation...")
    summaries = call_anthropic(stories_with_text, target_date_str)

    # Enforce hard cap: max 3 stories per category (safety net for prompt instruction)
    MAX_PER_CATEGORY = 3
    cat_counts = {}
    capped = []
    for s in summaries:
        cat = s.get("category", "other")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if cat_counts[cat] <= MAX_PER_CATEGORY:
            capped.append(s)
        else:
            print(f"  Dropped excess {cat} story: {s.get('headline', '?')[:60]}")
    if len(capped) < len(summaries):
        print(f"  Category cap trimmed {len(summaries)} -> {len(capped)} stories")
        summaries = capped

    # Step 4b: Get daily joke
    print("\n[Step 4b] Getting daily joke...")
    joke = get_daily_joke(target_date_str)
    if joke:
        print(f"  Joke: {joke['comedian']} -- {joke['joke'][:60]}...")
    else:
        print("  Joke: skipped (load failed)")

    # Step 4c: Fetch weather forecast
    print("\n[Step 4c] Fetching weather forecast...")
    weather = fetch_weather_forecast(target_date_str)
    if weather:
        print(f"  Weather: {weather['conditions']}, High {weather['high']}F / Low {weather['low']}F")
    else:
        print("  Weather: skipped (fetch failed or unavailable)")

    # Step 4d: Fetch weekend events (Friday only)
    weekend_events = None
    target_date_obj = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
    if target_date_obj.weekday() == 4:  # Friday
        print("\n[Step 4d] Fetching weekend events (it's Friday!)...")
        brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
        weekend_events = fetch_weekend_events(brave_key, target_date_str, freshness)
        if weekend_events:
            print(f"  Found {len(weekend_events)} weekend events")
        else:
            print("  Weekend events: skipped (fetch/curation failed)")
    else:
        print("\n[Step 4d] Skipping weekend events (not Friday).")

    # Step 4e: Fetch top world story
    print("\n[Step 4e] Fetching top world story...")
    top_story = fetch_top_world_story(brave_key, target_date_str)
    if top_story:
        print(f"  Top story: {top_story['headline'][:70]}...")
    else:
        print("  Top story: skipped (fetch/curation failed)")

    # Step 5: Write output
    print("\n[Step 5] Writing JSON output...")
    filepath = write_output(summaries, target_date_str, target_formatted,
                            joke=joke, weather=weather, weekend_events=weekend_events,
                            top_story=top_story)

    # Step 6: Send email (skip for backfill unless forced)
    if not args.date:
        print("\n[Step 6] Sending newsletter email...")
        send_email(summaries, target_date_str, target_formatted,
                   joke=joke, weather=weather, weekend_events=weekend_events,
                   top_story=top_story)
    else:
        print("\n[Step 6] Skipping email (backfill mode).")

    print("\nDone!")
    return filepath


if __name__ == "__main__":
    main()
