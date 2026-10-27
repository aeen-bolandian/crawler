# Google Maps Review Scraper

Async, resumable, Playwright-based scraper for extracting Google Maps reviews from cafes stored in a SQLite database. Attaches to an existing Chrome debug session via CDP to preserve your logged-in Google account, validates results by coordinates to avoid scraping the wrong cafe, and streams reviews to CSV.

---

## Features

- **Async multi-worker scraping** — N concurrent workers share a cafe queue
- **CDP-based** — attaches to Chrome via `--remote-debugging-port=9222`; preserves the logged-in Google session
- **Tab pooling** — reuses user-opened Maps tabs (avoids blank-page problem in CDP)
- **Coordinate validation** — haversine check with 500 m strict / 800 m loose thresholds
- **Persian ↔ Latin name fallback** — cross-script name matching when coordinates are unavailable
- **Resumable checkpointing** — only cafes with ≥ 1 review are marked processed
- **Skip reporting** — every skipped cafe recorded with a reason code (`data/skipped_cafes.csv`)
- **Retry mode** — `--retry-skipped` re-attempts only previously failed cafes
- **CSV output** — UTF-8 with BOM for Excel compatibility

---

## Prerequisites

- **Python** 3.10+
- **Google Chrome** installed
- **A Google account** logged into Google Maps (Maps shows "Limited View" to anonymous users)
- **Chrome debug session** started manually before running the scraper
- **N Maps tabs open** in that Chrome (N = `--workers`)

---

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

---

## Chrome Debug Setup (REQUIRED)

The scraper attaches to a running Chrome instance. You must start Chrome in debug mode first.

### 1. Close all regular Chrome windows

```bash
pkill -f chrome || true
sleep 2
```

### 2. Launch Chrome with debug port

```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-debug-gmap &
```

### 3. In that Chrome window

1. Log in to a Google account.
2. Open exactly N tabs at `https://www.google.com/maps` (N = `--workers`, default 4).
3. Wait until each map is fully visible (not a blank page, not a consent page).
4. Leave the tabs open — the crawler reuses them.

### 4. Verify CDP is reachable

```bash
curl -s http://localhost:9222/json/version
```

You should see JSON with `"Browser": "Chrome/..."`.

---

## Usage

### Basic crawl (all approved cafes, 4 workers)

```bash
python run.py --db cafes_cafe.db --workers 4
```

### Crawl with a limit

```bash
python run.py --db cafes_cafe.db --workers 4 --limit 50
```

### Resume automatically (state is preserved)

```bash
python run.py --db cafes_cafe.db --workers 4
# picks up where it left off based on crawler_state.json
```

### Retry only skipped cafes

```bash
python run.py --db cafes_cafe.db --workers 4 \
  --retry-skipped \
  --skipped-input data/skipped_cafes.csv
```

### Custom CDP endpoint

```bash
python run.py --db cafes_cafe.db --workers 4 \
  --cdp-url http://localhost:9223
```

---

## CLI Reference

| Flag | Default | Description |
|---|---|---|
| `--db` | `cafes_cafe.db` | SQLite database path (read-only) |
| `--output` | `data/google_reviews.csv` | Output CSV path |
| `--state` | `crawler_state.json` | Checkpoint state JSON |
| `--skipped` | `data/skipped_cafes.csv` | Skip report CSV |
| `--skipped-input` | `None` | Input CSV for `--retry-skipped` mode |
| `--limit` | `None` | Max cafes this run |
| `--workers` | `4` | Concurrent workers (must equal open Maps tabs) |
| `--cdp-url` | `http://localhost:9222` | Chrome debug endpoint |
| `--headful` | `False` | Use headful browser if CDP fails |
| `--retry-skipped` | `False` | Only retry cafes from `--skipped-input` |
| `--log-level` | `INFO` | Logging verbosity (DEBUG/INFO/WARNING/ERROR) |

---

## Output Schema

### `data/google_reviews.csv`

| Column | Example |
|---|---|
| `cafe_slug` | `1e11ca0c-976e-4aad-a651-b9c68852f16a` |
| `cafe_name` | `عمارت نیلی` |
| `city` | `تهران` |
| `author` | `Ali S. M` |
| `rating` | `5 stars` |
| `date` | `2 months ago` |
| `text` | Full review body (newlines collapsed) |

### `data/skipped_cafes.csv`

| Column | Example |
|---|---|
| `cafe_id` | `188` |
| `cafe_slug` | `1e11ca0c-...` |
| `cafe_name` | `عمارت نیلی` |
| `reason` | `place_name_mismatch` |

**Skip reasons:** `no_page_signal`, `no_result_matched_within_threshold`, `place_coords_mismatch`, `place_name_mismatch`, `reviews_tab_could_not_be_clicked`, `review_cards_never_appeared`, `navigation_to_result_url_failed`, `exception`, `zero_reviews`.

### `crawler_state.json`

```json
{"processed_slugs": ["slug1", "slug2", ...]}
```

---

## Project Structure

```
gmap-antigravity/
├── AGENTS.md                    # Antigravity project rules
├── .agents/
│   ├── rules/                   # Conditional rules (glob-based)
│   └── skills/                  # Multi-step workflows
├── README.md
├── requirements.txt
├── pytest.ini
├── .gitignore
├── src/
│   ├── __init__.py
│   ├── db_loader.py             # SQLite loader -> CafeRecord
│   ├── resolver.py              # builds Google Maps URLs + coords validation
│   ├── exporter.py              # CSV streaming exporter
│   ├── state.py                 # checkpointing
│   ├── skip_reporter.py         # skip CSV
│   └── scraper.py               # Playwright-based scraper
├── run.py                       # CLI orchestrator
└── tests/
    ├── test_db_loader.py
    ├── test_resolver.py
    ├── test_exporter.py
    ├── test_state.py
    ├── test_skip_reporter.py
    ├── test_scraper.py
    └── test_run.py
```

---

## Testing

Run the full unit test suite (no browser or network required):

```bash
./.venv/bin/pytest tests/ -v
```

Run the integration test (requires Chrome debug on port 9222):

```bash
./.venv/bin/pytest -m integration tests/
```

**Coverage:** 81 unit tests passing, 1 integration test (skipped by default).

---

## Architecture Notes

### Pipeline flow

```
DatabaseLoader
      ↓ (CafeRecord)
LocationResolver
      ↓ (ResolvedTarget with URL + fallbacks)
ScraperWorker (×N)  ←── claims Page from tab pool
      ↓ (List[ReviewRecord])
CSVExporter
      ↓
data/google_reviews.csv
```

### Validation strategy

Three independent gates. If any passes, the place is accepted:

1. **Clicked-result coordinates** — the `href` of a search result carries exact place coords in `!3d...!4d...`. Compared with DB coords using haversine. Threshold: **800 m**.
2. **Page-URL coordinates** — after navigation, `window.location.href` contains `/@lat,lng`. Threshold: **500 m** (stricter because the URL is authoritative).
3. **Name fallback** — if no coordinates available, compare normalized names with Persian↔Latin transliteration + n-gram matching.

If none pass, the cafe is skipped and logged with a reason code.

### Checkpointing

Only cafes with **≥ 1 extracted review** are added to `crawler_state.json`. This means:

- Interrupting with `Ctrl+C` never loses progress.
- Failed cafes are retried on the next run by default.
- To avoid re-attempting failed cafes every run, use `--retry-skipped` with an explicit `--skipped-input`.

### Rate limit awareness

Google aggressively rate-limits Maps scraping. Signs:

- Many `page.goto` timeouts in a row
- Tabs redirect to `https://www.google.com/sorry/...`
- Success rate drops below 30 %

**Recovery:**

1. `Ctrl+C` to stop (state is preserved).
2. Wait 30–60 minutes.
3. Close affected tabs, open 4 fresh Maps tabs.
4. Resume with the same command.

For large runs, split the database into shards and run on multiple IPs (each machine gets its own rate-limit quota).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `CDP endpoint unreachable` | Chrome debug not running | Start Chrome with `--remote-debugging-port=9222` |
| Tabs load blank | Forgot to open Maps tabs first | Open N tabs, restart crawl |
| Rate limit (timeouts) | Google throttling | `Ctrl+C`, wait 30–60 min, resume |
| `No result matched` | Bad address or place missing from Maps | Check `data/skipped_cafes.csv` |
| `Page pool empty` warning | Fewer tabs than `--workers` | Open more tabs or lower `--workers` |
| Extremely slow per cafe (> 30 s) | `wait_until="domcontentloaded"` hangs on SPA | Ensure `wait_until="commit"` in `_scrape_with_page` |
| `google.com/sorry` in tab URL | Captcha triggered | Solve manually in Chrome, wait 15 min |

---

## Development Conventions

- **AGENTS.md** + `.agents/rules/` — Antigravity guidelines (Python style, testing conventions)
- **Conventional Commits** — `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`
- **Line length** ≤ 88 (Black-compatible)
- **Type hints** required for all public functions
- **Tests** required for every new module (mark integration tests with `@pytest.mark.integration`)

---

## License

MIT
