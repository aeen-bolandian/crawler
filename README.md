# Google Maps Review Scraper

Async, resumable, Playwright-based scraper for extracting Google Maps reviews for cafes stored in a SQLite database. Attaches to an existing Chrome debug session via CDP to preserve your logged-in Google account, validates results by coordinates and name to avoid scraping the wrong venue, and streams reviews to CSV.

## Features

- Async multi-worker scraping (N concurrent workers)
- CDP-based: attaches to Chrome via --remote-debugging-port=9222 (preserves Google login)
- Tab pooling: reuses user-opened Maps tabs (avoids blank-page problem in CDP)
- Auto tab rotation: rotates tab every 10 cafes to prevent Chrome crash/OOM
- Coordinate validation: haversine check with 500m strict / 800m loose thresholds
- Persian ↔ Latin name fallback: cross-script transliteration and n-gram matching
- Rate-limit detection: pauses all workers 60s when google.com/sorry detected or 3 consecutive goto timeouts
- Retry logic: goto retries 3 times on timeout before skipping
- Resumable checkpointing: only cafes with >=1 review are marked processed
- Skip reporting: every skipped cafe recorded with a reason code
- Retry mode: --retry-skipped re-attempts only previously failed cafes
- Sharding: --shard-id / --num-shards hash-based distribution across machines
- CSV output: UTF-8 with BOM for Excel compatibility

## Prerequisites

- Python 3.10+
- Google Chrome installed
- A Google account logged into Google Maps
- Chrome debug session started manually

## Installation

1. Clone the repository:
   git clone git@github.com:aeen-bolandian/crawler.git
   cd crawler

2. Create virtual environment:
   python3 -m venv .venv
   source .venv/bin/activate

3. Install dependencies:
   pip install -r requirements.txt

4. Install Playwright Chromium (fallback browser):
   playwright install chromium

## Chrome Debug Setup (REQUIRED)

The scraper attaches to a running Chrome instance. You MUST start Chrome in debug mode before running the scraper.

1. Close all regular Chrome windows:
   pkill -f chrome || true
   sleep 2

2. Launch Chrome with debug port:
   google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug-gmap &

3. In that Chrome window:
   - Log in to a Google account
   - Open exactly N tabs at https://www.google.com/maps (N = --workers, default 4)
   - Wait until each map is fully visible (not blank, not a consent page)
   - Leave the tabs open — the crawler reuses them

4. Verify CDP is reachable:
   curl -s http://localhost:9222/json/version

You should see JSON with "Browser": "Chrome/...".

## Database Setup

Place your cafes_cafe.db (SQLite) in the project root:

Required table: cafes_cafe
Required columns: id, slug, name, address, city, neighborhood, latitude, longitude, phone, approval_status
Optional columns (improved matching): search_address, search_name, search_city

Verify schema:
sqlite3 cafes_cafe.db "PRAGMA table_info(cafes_cafe);"

## Usage

### Single machine (all approved cafes)

./.venv/bin/python run.py --db cafes_cafe.db --workers 4

### Limit to first N cafes

./.venv/bin/python run.py --db cafes_cafe.db --workers 4 --limit 50

### Resume automatically (state is preserved)

./.venv/bin/python run.py --db cafes_cafe.db --workers 4

### Retry only previously skipped cafes

./.venv/bin/python run.py --db cafes_cafe.db --workers 4 \
  --retry-skipped \
  --skipped-input data/skipped_cafes.csv

### Distributed across 15 machines

Each machine runs a different shard:

Machine 0:
./.venv/bin/python run.py --db cafes_cafe.db --workers 4 \
  --shard-id 0 --num-shards 15 \
  --output data/reviews_shard00.csv \
  --state crawler_state_shard00.json \
  --skipped data/skipped_shard00.csv

Machine 1:
./.venv/bin/python run.py --db cafes_cafe.db --workers 4 \
  --shard-id 1 --num-shards 15 \
  --output data/reviews_shard01.csv \
  --state crawler_state_shard01.json \
  --skipped data/skipped_shard01.csv

... and so on up to --shard-id 14.

Sharding is hash-based on cafe.slug (MD5), so each cafe always lands in the same shard — no duplicates across machines.

## CLI Reference

| Flag | Default | Description |
|---|---|---|
| --db | cafes_cafe.db | SQLite database path (read-only) |
| --output | data/google_reviews.csv | Output CSV path |
| --state | crawler_state.json | Checkpoint state JSON |
| --skipped | data/skipped_cafes.csv | Skip report CSV |
| --skipped-input | None | Input CSV for --retry-skipped |
| --limit | None | Max cafes this run |
| --workers | 4 | Concurrent workers (must equal open Maps tabs) |
| --shard-id | 0 | This shard's index (0-based) |
| --num-shards | 1 | Total number of shards |
| --cdp-url | http://localhost:9222 | Chrome debug endpoint |
| --headful | False | Use headful browser if CDP fails |
| --retry-skipped | False | Only retry cafes from --skipped-input |
| --log-level | INFO | Logging verbosity |

## Output Schema

### data/google_reviews.csv

| Column | Example |
|---|---|
| cafe_slug | 1e11ca0c-976e-4aad-a651-b9c68852f16a |
| cafe_name | عمارت نیلی |
| city | تهران |
| author | Ali S. M |
| rating | 5 stars |
| date | 2 months ago |
| text | Full review body (newlines collapsed) |

### data/skipped_cafes.csv

| Column | Example |
|---|---|
| cafe_id | 188 |
| cafe_slug | 1e11ca0c-... |
| cafe_name | عمارت نیلی |
| reason | place_name_mismatch |

Skip reasons: no_page_signal, no_result_matched_within_threshold, place_coords_mismatch, place_name_mismatch, reviews_tab_could_not_be_clicked, review_cards_never_appeared, navigation_to_result_url_failed, exception, zero_reviews.

### crawler_state.json

{"processed_slugs": ["slug1", "slug2", ...]}

## Batch Running (recommended for large crawls)

Run each shard in 100-cafe chunks with 5-minute pauses between chunks to avoid rate limiting. Example script (run_shard.sh):

#!/usr/bin/env bash
SHARD_ID="${1:?Usage: ./run_shard.sh <shard_id> <num_shards>}"
NUM_SHARDS="${2:?Usage: ./run_shard.sh <shard_id> <num_shards>}"
for chunk in $(seq 1 5); do
    ./.venv/bin/python run.py --db cafes_cafe.db --workers 4 \
        --shard-id "$SHARD_ID" --num-shards "$NUM_SHARDS" \
        --output "data/reviews_shard$(printf '%02d' $SHARD_ID).csv" \
        --state "crawler_state_shard$(printf '%02d' $SHARD_ID).json" \
        --skipped "data/skipped_shard$(printf '%02d' $SHARD_ID).csv" \
        --limit 100 --log-level INFO
    [ "$chunk" -lt 5 ] && sleep 300
done

Usage:
chmod +x run_shard.sh
./run_shard.sh 0 15

## Merging Shard Outputs

After all 15 shards finish:

python3 -c "
import csv, glob
shards = sorted(glob.glob('data/reviews_shard[0-9][0-9].csv'))
all_rows = []
columns = None
for shard in shards:
    with open(shard, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if columns is None:
            columns = reader.fieldnames
        rows = list(reader)
        all_rows.extend(rows)
    print(f'{shard}: {len(rows)} reviews')
seen = set()
unique = []
for row in all_rows:
    key = (row['cafe_slug'], row['author'], row['date'], row['text'])
    if key not in seen:
        seen.add(key)
        unique.append(row)
with open('data/google_reviews.csv', 'w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=columns)
    writer.writeheader()
    writer.writerows(unique)
print(f'Merged: {len(unique)} unique reviews')
"

## Architecture

Pipeline flow:
DatabaseLoader -> LocationResolver -> ScraperWorker (xN via tab pool) -> CSVExporter

Validation gates (any -> accept):
1. Clicked-result coordinates (href !3d/!4d) — 800m loose threshold
2. Page-URL coordinates (/@lat,lng) — 500m strict threshold
3. Name fallback — Persian/Latin transliteration + n-gram matching

Checkpointing: only cafes with >=1 review are marked processed. Failed cafes retry on next run.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| CDP endpoint unreachable | Chrome debug not running | Start Chrome with --remote-debugging-port=9222 |
| Tabs load blank | Missing Maps tabs | Open N tabs, restart crawl |
| Rate limit (timeouts) | Google throttling | Ctrl+C, wait 30-60 min, resume |
| Chrome page crashed | Tab OOM after many cafes | Tabs auto-rotate every 10 cafes |
| No result matched | Bad address or missing place | Check data/skipped_cafes.csv |
| Page pool empty | Fewer tabs than --workers | Open more tabs or lower --workers |

## License

MIT
