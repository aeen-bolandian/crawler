# Antigravity Crawler

An asynchronous, modular web crawler written in Python that provides efficient BFS crawling, intelligent rate limiting, and robust HTML extraction. Built with `httpx`, `selectolax`, and standard library SQLite, it is designed for scalable, respectful, and reliable data collection.

## Features

- **Async HTTP Fetching**: Powered by `httpx.AsyncClient` with automatic retries, exponential backoff, jitter, and configurable timeouts.
- **Fast HTML Parsing**: High-performance HTML parsing and text extraction using `selectolax` (Lexbor engine).
- **SQLite-Backed Page Store**: Thread-safe persistent storage using `asyncio.to_thread` with automatic upserts (`INSERT OR REPLACE`) keyed by URL.
- **BFS Crawling**: Level-by-level breadth-first traversal with concurrency limits, configurable max pages, and maximum depth bounds.
- **URL Normalization**: Canonicalizes URLs by lowercasing scheme/host, stripping fragments, resolving relative paths, and preserving subpath slashes and query strings.
- **Follows HTTP Redirects**: Transparently follows HTTP 3xx redirects during fetching.
- **Respects robots.txt**: Domain-level caching of `robots.txt` rules with automated checking and support for standard and fractional `Crawl-delay` directives.
- **Per-Domain Rate Limiting**: Domain-specific request rate limiting synchronized using individual `asyncio.Lock` primitives.
- **Sitemap.xml Support**: Seeds crawler queues from XML sitemaps, nested `<sitemapindex>` hierarchies (up to 3 levels deep), and gzip-compressed (`.xml.gz`) archives.
- **CLI Subcommands**: Command-line interface offering `crawl`, `show`, and `stats` subcommands.

## Project Structure

```text
antigravity-crawler/
├── AGENTS.md                 # Project agent rules and architectural guidelines
├── .agents/
│   ├── rules/                # Conditional rules and linting standards
│   └── skills/               # Reusable agent skills
├── data/
│   └── crawl.db              # Default SQLite database location
├── src/
│   ├── __init__.py           # Package marker
│   ├── crawler.py            # BFS traversal orchestrator and crawl lifecycle
│   ├── fetcher.py            # Async HTTP fetching with retry logic
│   ├── main.py               # CLI entrypoint and subcommand dispatchers
│   ├── parser.py             # HTML parsing, link extraction, text cleaning, URL normalization
│   ├── ratelimit.py          # Per-domain rate limiter with asyncio locks
│   ├── robots.py             # Robots.txt parser and rule cache
│   ├── sitemap.py            # XML sitemap parser and recursive extractor
│   └── storage.py            # SQLite page storage engine with async delegation
├── tests/
│   ├── fixtures/             # HTML fixture files (simple, relative links, noise)
│   ├── test_crawler.py       # Integration tests for crawler orchestration
│   ├── test_fetcher.py       # Fetching, retry, and redirect tests
│   ├── test_main.py          # CLI subcommand and argument parser tests
│   ├── test_parser.py        # Parser, title extraction, and URL normalization tests
│   ├── test_ratelimit.py     # Domain concurrency and delay spacing tests
│   ├── test_robots.py        # Robots.txt parsing and crawl delay tests
│   ├── test_sitemap.py       # Sitemap parsing, sitemapindex, and gzip tests
│   └── test_storage.py       # Database CRUD and async concurrency tests
├── pytest.ini                # Pytest configuration (pythonpath)
├── requirements.txt          # Python dependencies
└── README.md                 # Project documentation
```

## Installation

1. Create and activate a Python virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage Examples

The CLI provides three subcommands: `crawl`, `show`, and `stats`. Commands can be executed via `python3 src/main.py <subcommand>` (or `python src/main.py <subcommand>` inside an active virtual environment).

### 1. Crawl Web Pages

Start a crawl using a seed URL:
```bash
python3 src/main.py crawl --seed https://example.com --max-pages 20 --max-depth 2 --concurrency 3 --rate-limit 1.0
```

Start a crawl seeded from an XML sitemap:
```bash
python3 src/main.py crawl --sitemap https://example.com/sitemap.xml --max-pages 50 --concurrency 5
```

Crawl seeded from a gzip-compressed sitemap:
```bash
python3 src/main.py crawl --sitemap https://example.com/sitemap.xml.gz
```

Restrict crawl traversal strictly to the seed domain:
```bash
python3 src/main.py crawl --seed https://example.com --same-domain-only
```

Bypass robots.txt checks and crawl-delay directives:
```bash
python3 src/main.py crawl --seed https://example.com --ignore-robots
```

Store pages in a custom SQLite database path:
```bash
python3 src/main.py crawl --seed https://example.com --db data/my_crawl.db
```

### 2. Display Stored Page Details

Retrieve and view the title, fetched timestamp, and text snippet of a saved page:
```bash
python3 src/main.py show --url https://example.com/
```

Specify a custom database location:
```bash
python3 src/main.py show --url https://example.com/ --db data/my_crawl.db
```

### 3. Display Storage Statistics

View the total number of pages stored in the database:
```bash
python3 src/main.py stats
```

Specify a custom database location:
```bash
python3 src/main.py stats --db data/my_crawl.db
```

## CLI Reference

### `crawl` Subcommand

| Flag | Default | Description |
| --- | --- | --- |
| `--seed URL` | None | Seed URL to start BFS crawling (mutually exclusive with `--sitemap`, exactly one required) |
| `--sitemap URL` | None | Sitemap URL to seed crawl queue (mutually exclusive with `--seed`, exactly one required) |
| `--max-pages N` | `10` | Maximum number of pages to crawl |
| `--max-depth N` | `2` | Maximum link crawl depth from seed |
| `--concurrency N` | `3` | Maximum concurrent HTTP requests |
| `--db PATH` | `data/crawl.db` | Path to SQLite database file |
| `--same-domain-only` | `False` | Restrict crawling strictly to seed URL domain |
| `--ignore-robots` | `False` | Ignore robots.txt rules and crawl-delay directives |
| `--rate-limit S` | `1.0` | Minimum delay in seconds between requests to the same domain |

### `show` Subcommand

| Flag | Default | Description |
| --- | --- | --- |
| `--url URL` | None (Required) | Absolute URL of the stored page to display |
| `--db PATH` | `data/crawl.db` | Path to SQLite database file |

### `stats` Subcommand

| Flag | Default | Description |
| --- | --- | --- |
| `--db PATH` | `data/crawl.db` | Path to SQLite database file |

## Testing

Run the full test suite using `pytest`:
```bash
./venv/bin/pytest tests/ -v
```

All 84 tests cover unit and integration requirements across modules:
- Fetching retry mechanisms, status code handling, and redirects (`test_fetcher.py`)
- HTML link extraction, text cleaning, and URL normalization (`test_parser.py`)
- SQLite storage schema, async operations, and upserts (`test_storage.py`)
- Robots.txt parsing, cache lookups, and crawl-delay values (`test_robots.py`)
- Domain rate limiter spacing, zero delay, and concurrency (`test_ratelimit.py`)
- XML sitemaps, sitemapindex recursion, and gzip decompression (`test_sitemap.py`)
- BFS crawler orchestration and limits (`test_crawler.py`)
- CLI execution, arguments, and exit codes (`test_main.py`)

## Architecture Notes

- **BFS Level-by-Level Traversal**: The crawler queue maintains `(url, depth)` pairs and processes them in FIFO order using an `asyncio.Queue`. Concurrency is bounded by an `asyncio.Semaphore`. Discovered links are normalized and checked against the database before enqueuing to prevent duplicate fetches.
- **Per-Domain Rate Limiter**: Implemented in `src/ratelimit.py` (`DomainRateLimiter`) using per-domain `asyncio.Lock` primitives and monotonic clock tracking (`time.monotonic()`). Requests to distinct domains execute concurrently, while requests to the same domain are spaced out by the effective delay.
- **Robots.txt Cache**: `RobotsCache` in `src/robots.py` fetches and parses `robots.txt` once per domain using standard library `urllib.robotparser`. When `respect_robots` is enabled, paths are checked prior to dispatch, and `Crawl-delay` directives dynamically override default rate limiting if greater than `--rate-limit`.
- **Redirect Handling**: Configured via `follow_redirects=True` in `httpx.AsyncClient` within `src/fetcher.py`. Fetches follow 3xx redirects to completion, and extracted links resolve against target locations.
- **SQLite Storage Schema**: The `PageStore` in `src/storage.py` delegates blocking SQLite operations to worker threads via `asyncio.to_thread`. The database schema is defined as:

```sql
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status INTEGER NOT NULL
);
```

Pages are persisted using `INSERT OR REPLACE INTO pages`, ensuring duplicate visits update records cleanly without primary key collisions.

## Development Conventions

- **Code Style**: Strictly adheres to PEP 8, with function signatures fully typed and docstrings formatted per PEP 257.
- **Logging**: Uses `loguru` (with standard library `logging` fallback) for all log outputs. Direct `print()` statements are avoided in core library modules.
- **Antigravity Rules**: System rules and architecture guidelines are enforced in `AGENTS.md` and `.agents/rules/`.
- **Conventional Commits**: Git commits follow the Conventional Commits specification (e.g., `feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
