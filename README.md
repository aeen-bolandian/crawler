# Google Maps Review Scraper

Run one command, get a CSV of Google Maps reviews for your assigned cafe shard.

---

## What You Need

- **Python 3.10+**
- **Google Chrome** installed
- **A Google account** (to log into Google Maps in Chrome)
- **Your personal `cafes_cafe.db` shard** (provided separately by the project owner)
- **Your assigned shard number N** (from `0` to `14`, for 15 total shards)

---

## Step-by-Step Setup

### 1. Clone the repository

```bash
git clone git@github.com:aeen-bolandian/crawler.git
cd crawler
```

### 2. Create virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Place your `cafes_cafe.db` in the project root

Copy the SQLite file you received into the root directory of this repository:

```bash
# Verify the database is in the root directory
ls -la cafes_cafe.db
```

### 4. Start Chrome with remote debugging

Close any existing Chrome instances first, then launch with debug port `9222`:

```bash
# On Linux:
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile" "https://www.google.com/maps" &

# On macOS:
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile" "https://www.google.com/maps" &
```

### 5. Log into Google Maps

In the Chrome browser window that opens:
- Log into your Google account.
- Accept any Google consent dialogs if prompted.

### 6. Open 4 Google Maps tabs

The scraper uses 4 concurrent workers and attaches to existing open tabs:
- Ensure you have **at least 4 tabs** open to `https://www.google.com/maps`.
- Wait until each map is fully loaded (not blank, not stuck on consent).
- Leave Chrome running in the background.

### 7. Run the crawler

Run the helper script with your assigned shard number `N` (e.g. `0` if you were assigned shard 0) and total shards `15`:

```bash
./run_shard_with_restart.sh N 15
```

> **Note:** Replace `N` with your assigned shard number (`0` to `14`).

The script automatically processes your shard in chunks of 50 cafes, restarts Chrome between chunks to prevent memory bloat, checkpointing progress after each cafe.

### 8. Get your output CSV

When the script finishes, your reviews are saved under `data/`:
- `data/reviews_shardNN.csv` (e.g., `data/reviews_shard00.csv`)
- `data/skipped_shardNN.csv` (optional skip log)

---

## Run Command (Quick Reference)

```bash
./run_shard_with_restart.sh N 15
```

Replace `N` with your assigned shard number (`0` through `14`).

---

## What You Get

- `data/reviews_shardNN.csv` — **Send this back to the project owner.**
- `data/skipped_shardNN.csv` — Optional skip log detailing venues not found or skipped.

---

## Troubleshooting

- **Chrome crash / OOM:**
  The scraper automatically rotates tabs every 10 cafes and restarts Chrome between 50-cafe chunks. If Chrome terminates unexpectedly, simply re-run `./run_shard_with_restart.sh N 15` — state is preserved and already scraped cafes are skipped.
- **Rate limit ("pausing all workers for 60s" or Google block):**
  If Google temporarily slows down responses, wait 30–60 minutes, then re-run the same command. Checkpointing ensures no duplicate work.
- **CDP endpoint unreachable (`connect ECONNREFUSED 127.0.0.1:9222`):**
  Chrome is not running with `--remote-debugging-port=9222`. Re-run Step 4 to start Chrome in debug mode.
- **Page signal timeout:**
  Google Maps took longer than usual to render. The crawler will retry automatically or fall back to bare search. If your connection is slow, re-running will resume smoothly.

---

## How to Send Results Back

Once your shard finishes, send your output file:
- Send `data/reviews_shardNN.csv` to the project owner via **Telegram** or **Google Drive**.

---

## Security: Do NOT Commit

Never commit sensitive files to Git:
- **Do NOT commit** your `cafes_cafe.db` or any `*.db` files.
- **Do NOT commit** any output CSV files (`data/*.csv`, `*.csv`).
- **Do NOT commit** state files (`crawler_state*.json`).

All these files are strictly ignored by `.gitignore`.
