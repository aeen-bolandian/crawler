# Google Maps Review Scraper

An asynchronous, resumable, Playwright-based scraper for Google Maps cafe reviews. It attaches to an existing Google Chrome debugging session via Chrome DevTools Protocol (CDP) to preserve active logins, uses tab pooling to prevent blank page loads, validates venues by coordinates and Persian↔Latin transliteration, and exports reviews into Excel-compatible CSVs.

---

## Features

- **Async Multi-Worker Scraping**: Spawns $N$ concurrent workers sharing a unified cafe queue and tab pool.
- **CDP-Based Browser Automation**: Attaches to a running Chrome instance (`--remote-debugging-port=9222`), preserving authenticated Google sessions and avoiding automated bot detection.
- **Tab Pooling**: Reuses existing user-opened Maps tabs from an `asyncio.Queue` tab pool to circumvent CDP blank page creation issues.
- **Strict Coordinate Validation**: Matches venues using Haversine distance (500m strict threshold for page URLs, 800m loose threshold for search result hrefs).
- **Persian↔Latin Transliteration Fallback**: Normalizes Persian characters, maps architectural tokens (e.g., "عمارت" ↔ "Mansion"), and uses phonetic matching when coordinates are absent.
- **Resumable Checkpointing**: Tracks state in `crawler_state.json` via batched atomic writes (`os.replace`), only recording cafes with $\ge 1$ review.
- **Skip Reporting**: Records un-scraped venues and exact error/skip reasons to `data/skipped_cafes.csv`.
- **Targeted Retry Mode**: Supports `--retry-skipped` with `--skipped-input` to re-attempt failed venues without rescraping completed ones.
- **Excel-Compatible CSV Export**: Streams reviews safely under an `asyncio.Lock` using `utf-8-sig` encoding (UTF-8 with BOM) and collapsed newlines.

---

## Prerequisites

1. **Python 3.10+** installed on Linux / macOS / Windows.
2. **Google Chrome** installed.
3. **Google Account** logged in to Google Maps in Chrome.
4. **Active Chrome Debug Session**:
   Launch Chrome with remote debugging and a dedicated profile directory:
   ```bash
   google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug-crawler
   ```
5. **Open Tabs**:
   In that opened Chrome window, navigate to `https://www.google.com/maps` and open exactly $N$ tabs (where $N$ equals your `--workers` count, default 4). Log in to your Google account in this window.

---

## Installation

1. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Install Playwright Chromium browser binaries (for fallback execution):
   ```bash
   playwright install chromium
   ```

---

## Usage

### 1. Basic Execution
Scrape approved cafes from `cafes_cafe.db` using 4 concurrent workers:
```bash
python run.py --db cafes_cafe.db --workers 4
```

### 2. Limit Number of Cafes
Process only the first 50 pending cafes:
```bash
python run.py --db cafes_cafe.db --workers 4 --limit 50
```

### 3. Automatically Resume
Interrupted runs resume seamlessly without duplicate scrapes (already completed cafes in `crawler_state.json` are skipped):
```bash
python run.py --db cafes_cafe.db
```

### 4. Retry Previously Skipped Cafes
Target only cafes logged in the skip report:
```bash
python run.py --retry-skipped --skipped-input data/skipped_cafes.csv
```

### 5. Custom CDP Endpoint
Attach to Chrome running on an alternate debugging port:
```bash
python run.py --cdp-url http://localhost:9223
```

---

## CLI Flags Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--db` | `cafes_cafe.db` | Path to read-only SQLite database file |
| `--output` | `data/google_reviews.csv` | Destination CSV file for scraped reviews |
| `--state` | `crawler_state.json` | JSON checkpoint file tracking completed slugs |
| `--skipped` | `data/skipped_cafes.csv` | CSV report path for logging skipped cafes |
| `--skipped-input` | `None` | Source skip CSV file when running in `--retry-skipped` mode |
| `--limit` | `None` | Maximum number of cafes to process during this execution |
| `--workers` | `4` | Number of concurrent workers (must equal open Maps tabs) |
| `--cdp-url` | `http://localhost:9222` | Chrome DevTools Protocol remote debugging endpoint |
| `--headful` | `False` | Run headful browser if local Chromium launch fallback occurs |
| `--retry-skipped` | `False` | Restrict processing exclusively to slugs from `--skipped-input` |
| `--log-level` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Output Schema

### 1. Reviews Data (`data/google_reviews.csv`)
Encoded in UTF-8 with BOM (`utf-8-sig`) with collapsed newlines for full Microsoft Excel compatibility:
- `cafe_slug`: Unique slug identifier from SQLite.
- `cafe_name`: Name of the cafe.
- `city`: City location.
- `author`: Reviewer display name.
- `rating`: Rating string (e.g. `5 stars`).
- `date`: Relative or absolute review post date.
- `text`: Review text content with whitespace and newlines collapsed.

### 2. Skipped Venues Report (`data/skipped_cafes.csv`)
- `cafe_id`: Numeric database primary key.
- `cafe_slug`: Unique cafe slug.
- `cafe_name`: Venue name.
- `reason`: Machine-readable reason code from `SkipReason` (e.g. `place_coords_mismatch`, `zero_reviews`).

### 3. Checkpoint State (`crawler_state.json`)
Atomic JSON dictionary flushed periodically and upon clean shutdown:
```json
{
  "processed_slugs": [
    "cafe-bahar",
    "cafe-nili",
    "cafe-tehran"
  ]
}
```

---

## Project Structure

```text
gmap-antigravity/
├── .agents/
│   └── rules/                # System agent rules and linters
├── AGENTS.md                 # Project architecture standards
├── data/                     # Ignored output directory
│   ├── google_reviews.csv    # Scraped customer reviews
│   └── skipped_cafes.csv     # Skip and failure report
├── logs/                     # Rotating crawler log files
├── src/
│   ├── __init__.py           # Package marker
│   ├── db_loader.py          # Read-only SQLite database loader
│   ├── exporter.py           # Async-safe CSV exporter with BOM
│   ├── resolver.py           # Search query builder and coordinate math
│   ├── scraper.py            # Playwright CDP tab pool and review extractor
│   ├── skip_reporter.py      # Skipped venue tracking and reload
│   └── state.py              # Batched atomic state checkpointing
├── tests/
│   ├── __init__.py           # Test package marker
│   ├── test_db_loader.py     # SQLite loader tests
│   ├── test_exporter.py      # CSV exporter concurrency tests
│   ├── test_resolver.py      # Query resolution and coordinate tests
│   ├── test_run.py           # CLI orchestrator tests
│   ├── test_scraper.py       # Scraper unit and CDP fallback tests
│   ├── test_skip_reporter.py # Skip reporting tests
│   └── test_state.py         # Checkpointing and atomic write tests
├── .gitignore                # Git ignore rules
├── pytest.ini                # Pytest configuration
├── requirements.txt          # Python dependencies
├── run.py                    # CLI orchestrator entrypoint
└── README.md                 # Project documentation
```

---

## Testing

Run the full automated test suite using `pytest`:
```bash
./.venv/bin/pytest tests/ -v
```

The test suite includes 81 unit tests that run completely offline without needing a browser or network access:
- **`test_db_loader.py`**: SQLite schema validation, read-only mode, and optional columns.
- **`test_resolver.py`**: Address trimming, Persian stop word removal, Haversine formula, and coordinate regex.
- **`test_exporter.py`**: UTF-8 BOM encoding, CSV row escaping, newline collapsing, and concurrent appends.
- **`test_state.py`**: Batched threshold writes, corruption handling, and atomic replacements (`os.replace`).
- **`test_skip_reporter.py`**: Skip CSV generation and slug re-loading for retry mode.
- **`test_scraper.py`**: Transliteration matching, tab acquisition/release, coordinate validation, and selector fallbacks.
- **`test_run.py`**: Argument parsing, limit constraints, worker loops, and cancellation handling.

To run the live integration test (requires a running Chrome debug session on port 9222):
```bash
./.venv/bin/pytest -m integration tests/
```

---

## Architecture & Design Notes

1. **Pipeline Lifecycle**:
   `DatabaseLoader` queries approved cafes $\rightarrow$ `LocationResolver` constructs primary and fallback URLs $\rightarrow$ `CrawlerPipeline` dispatches tasks across `ScraperWorker`s via the tab pool $\rightarrow$ `CSVExporter` streams reviews under `asyncio.Lock` $\rightarrow$ `StateManager` flushes processed slugs.
2. **Tab Pool Management**:
   Playwright's `context.new_page()` frequently yields blank pages when attached to Chrome over CDP. `GoogleMapsScraper` populates an `asyncio.Queue` with user-opened Maps tabs and recycles them safely between workers.
3. **Two-Tier Validation**:
   - Primary: Haversine distance $\le 500\text{m}$ for page URLs, $\le 800\text{m}$ for search result links.
   - Fallback: Persian Unicode normalization and phonetic transliteration matching for venues lacking GPS coordinates.
4. **Rate Limit Resilience**:
   Detects `google.com/sorry` redirects and pauses gracefully. Google typically throttles review requests after ~50 venues per IP address. If rate-limited, terminate with `Ctrl+C`, wait 30–60 minutes, and resume by re-running `python run.py --db cafes_cafe.db`.

---

## Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| CDP endpoint unreachable | Chrome debug session is not running | Start Chrome with `google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug-crawler` |
| Tabs load blank | Missing pre-opened Google Maps tabs | Open $N$ tabs navigating to `https://www.google.com/maps` in the debug Chrome window, then restart `run.py` |
| Rate limiting / timeouts | Google IP throttling (`google.com/sorry`) | Press `Ctrl+C` to cleanly flush state, wait 30–60 minutes, and resume execution |
| Venue skipped / no result matched | Inaccurate address or venue not found on Maps | Inspect `data/skipped_cafes.csv` to review the skip reason code |

---

## Development Conventions

- Strictly complies with PEP 8 and line lengths $\le 88$.
- Full type annotations on all function and method signatures.
- Detailed docstrings adhering to PEP 257.
- Loguru logging throughout (`logger.info`, `logger.warning`, `logger.debug`); no bare `print()` statements.
- Atomic commits following the Conventional Commits specification (`feat:`, `chore:`, `docs:`).
