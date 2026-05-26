# Changelog

All notable changes to the Denver Digest (303 News) project will be documented in this file.

Versioning follows the date format: `vYYYY.M.D`

## v2026.5.26

### Changed: Daily fire time moved to 6:00 AM Denver local, year-round

The GitHub Actions schedule cron (backup trigger) was firing at 6:15 AM Denver
local. Moved to 6:00 AM exactly so the email arrives at ~6:00 AM on the dot.

**Workflow changes:**
- `12:00 UTC` cron (was `12:15`) -> 6:00 AM MDT during summer
- `13:00 UTC` cron (was `13:15`) -> 6:00 AM MST during winter

**Script changes:**
- `check_denver_time()` rewritten. Old logic was buggy: it had a strict
  UTC-hour-based DST guard tied to the old 11:45/12:45 UTC cron times that
  no longer matched the actual `15 12 / 15 13` cron entries. During MDT,
  the guard would have rejected BOTH cron fires (including the correct
  one), so the backup was effectively non-functional during summer.
- New guard uses Denver local hour directly. Allows any fire between
  5:30 AM and 7:30 AM Denver local time (covers both DST states' 6:00 AM
  cron fires plus ~90 minutes of cron drift). Outside that window, exit.
- The script's `check_already_generated` continues to handle the case
  where both the primary (cron-job.org) and backup (GitHub schedule)
  fire on the same day -- the second one sees the file exists, exits.

**For 6:00 AM on the dot year-round, update cron-job.org primary trigger to:**
- Timezone: `America/Denver`
- Cron: `0 6 * * *` (or `55 5 * * *` if you want the email to actually
  ARRIVE at 6:00 AM after ~3-5 min of script processing)

This way cron-job.org handles DST automatically and fires once per day at
exactly 6:00 AM local time.

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
