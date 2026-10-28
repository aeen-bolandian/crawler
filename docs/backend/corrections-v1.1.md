# Crawler Fixes & Network Stability Guide (v1.1)

## 1. Overview & Problem Summary

During shard crawling on remote nodes (such as shard 5), three primary operational issues were observed:

1. **Duplicate Reviews in CSV Output**: Reviews being appended multiple times on crawler restarts or re-evaluations.
2. **`NameError: name 'open' is not defined` on Interrupt (Ctrl+C)**: During shutdown, Python's runtime teardown unbinds built-in functions, causing `state.flush()` in the `finally:` block to crash when calling `open()`.
3. **`no_page_signal` and `SSL handshake failed` (`net_error -100`)**: Frequent socket errors emitted by Chromium during page navigation.

---

## 2. Root Cause & Implemented Solutions

### Problem 1: Review Duplication
- **Root Cause**: Rerunning batches after partial failures wrote rows already appended, and duplicate cards loaded in dynamic DOM scrolling were evaluated repeatedly.
- **Solution**:
  - `src/scraper.py`: DOM review extractor uses a 4-tuple key `(author, date, text, rating)` to eliminate duplicate cards before returning.
  - `src/exporter.py`: `CSVExporter` maintains an in-memory `_seen` set tracking `(cafe_slug, author, date, text)` to reject duplicate writes.
  - `run.py`: `state.mark_processed(cafe.slug)` is awaited *before* `exporter.append_many(reviews)`.

### Problem 2: `NameError: open` on Process Termination
- **Root Cause**: Calling bare `open()` in `StateManager._flush_locked()` fails if Python has initiated garbage collection or module destruction during `SIGINT` / `KeyboardInterrupt`.
- **Solution**:
  - Replaced all calls to `open()` in `src/state.py` with `Path.write_text()` and `Path.read_text()`.
  - Wrapped `_flush_locked()` in an all-encompassing `try/except Exception` block that never raises errors during shutdown.
  - Added a dedicated, self-contained `_emergency_state_flush()` handler in `run.py` registered with `atexit`, importing dependencies dynamically via `__import__("json")` and `__import__("os")`.

### Problem 3: Network & SSL Handshake Failures
- **Root Cause**: `net/socket/ssl_client_socket_impl.cc:949 handshake failed; net_error -100` and `no_page_signal` are caused by local proxy/VPN instability, ISP throttling, or packet resets, **not application code errors**.
- **Solution**:
  - Added `_is_network_error(err)` helper in `src/scraper.py` detecting SSL handshake failures, connection resets, and Chromium socket errors (`net_error -100`, `net_error -2`).
  - Applied SSL detection to **every** `page.goto()` invocation across the crawler (`_scrape_with_page_impl`, `_retry_with_bare_name`, `_rotate_tab`).
  - When detected, the crawler immediately logs `"[network] SSL handshake failed — check VPN"` and skips the cafe without burning 3 retry attempts or misidentifying the issue as Google rate-limiting.
  - Added a pre-flight startup network check `_check_network()` in `run.py` that verifies Google Maps reachability before opening the database or launching tabs.

---

## 3. Operator Pre-flight Network Verification

Before starting or resuming any shard crawl, contributors must verify that their connection / VPN has an unobstructed route to Google Maps.

Run this check in your terminal:

```bash
curl -s https://www.google.com/maps -o /dev/null -w "%{http_code}\n"
```

- **If output is `200` or `301`/`302`**: Your network route to Google Maps is functional. You can proceed with the crawl.
- **If output is `000`, times out, or reports SSL/TLS errors**: **DO NOT run the crawler.** Your VPN or internet connection is unstable. Fix the network connection first before crawling.
