# Changelog

All notable changes to the Denver Digest (303 News) project will be documented in this file.

Versioning follows the date format: `vYYYY.M.D`

## v2026.4.13

### Fixed: Cross-day story deduplication

Old stories were appearing in the daily digest as if they were new. Three repeat stories appeared in the April 13 digest that had already been covered in prior days.

**Root causes identified:**

- Stories with homepage-only URLs (e.g. `denvergazette.com/` instead of a specific article path) bypassed the date filter, which relies on extracting dates from URL paths
- The update indicator word list was too broad -- words like "killed", "arrested", "dead" appear in first-day crime reporting just as often as in genuine updates, causing the cross-day dedup filter to incorrectly allow stale stories through
- The lookback window was only 3 days, so stories from 4+ days ago could reappear

**Changes:**

- Expanded cross-day dedup lookback from 3 days to 7 days
- Pruned update indicator words to only include terms that genuinely signal follow-up reporting (convicted, verdict, sentenced, lawsuit, etc.) and removed generic crime words (killed, dead, arrested, charged, etc.)
- Added homepage URL detection -- stories with no article path in the URL now face stricter similarity thresholds (0.40 vs 0.55) and keyword overlap thresholds (3 vs 4) since they are more likely stale search snippets
- Added strict mode for keyword-overlap dedup matches -- when stories share 4+ significant keywords (same event), only explicit update-signal words can bypass the filter; different wording of the same facts no longer counts as a "new angle"
- Strengthened the Claude curation prompt with more explicit dedup rules: different sources or slight rewording do NOT qualify as updates; genuine updates MUST be prefixed with "Update: "
