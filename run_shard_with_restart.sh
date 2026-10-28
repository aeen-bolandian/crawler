#!/usr/bin/env bash
# run_shard_with_restart.sh
# Usage: ./run_shard_with_restart.sh <shard_id> <num_shards>

set -euo pipefail

SHARD_ID="${1:?Usage: ./run_shard_with_restart.sh <shard_id> <num_shards>}"
NUM_SHARDS="${2:?Usage: ./run_shard_with_restart.sh <shard_id> <num_shards>}"

CHUNK=50
NUM_CHUNKS=10
SLEEP_BETWEEN=600
WORKERS=2

# Auto-detect database file
DB=""
if [ -f "cafes_cafe.db" ]; then
    DB="cafes_cafe.db"
elif ls cafes_cafe_shard*.db 1>/dev/null 2>&1; then
    DB=$(ls cafes_cafe_shard*.db 2>/dev/null | head -1)
    echo "📁 Auto-detected database: $DB"
elif ls *.db 1>/dev/null 2>&1; then
    DB=$(ls *.db 2>/dev/null | head -1)
    echo "⚠ Using database (auto-detected): $DB"
else
    echo "❌ ERROR: No database file (*.db) found in project root."
    exit 1
fi

echo "📁 Database: $DB"
echo "🔢 Shard: $SHARD_ID/$NUM_SHARDS"
echo "⚙️  Workers: $WORKERS"
echo "⏸  Sleep between chunks: ${SLEEP_BETWEEN}s"

OUTPUT="data/reviews_shard$(printf '%02d' $SHARD_ID).csv"
STATE="crawler_state_shard$(printf '%02d' $SHARD_ID).json"
SKIPPED="data/skipped_shard$(printf '%02d' $SHARD_ID).csv"

for chunk in $(seq 1 $NUM_CHUNKS); do
    echo ""
    echo "════════════════════════════════════════════════════"
    echo "🔄 Chunk $chunk/$NUM_CHUNKS — restarting Chrome"
    echo "════════════════════════════════════════════════════"

    pkill -f "remote-debugging-port=9222" || true
    sleep 3

    google-chrome \
        --remote-debugging-port=9222 \
        --user-data-dir="$HOME/chrome-debug-profile" \
        "https://www.google.com/maps" &
    sleep 5

    for i in 1; do
        curl -s -X PUT "http://localhost:9222/json/new?https://www.google.com/maps" >/dev/null
        sleep 1
    done

    echo "⏳ Waiting for Maps tabs to load (10s)..."
    sleep 10

    ./.venv/bin/python run.py \
        --db "$DB" \
        --workers "$WORKERS" \
        --shard-id "$SHARD_ID" --num-shards "$NUM_SHARDS" \
        --output "$OUTPUT" \
        --state "$STATE" \
        --skipped "$SKIPPED" \
        --limit "$CHUNK" \
        --log-level INFO

    echo ""
    echo "📊 After chunk $chunk:"
    python3 -c "
import csv, json
try:
    with open('$OUTPUT', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
        slugs = {r['cafe_slug'] for r in rows}
    print(f'   cafes: {len(slugs)}')
    print(f'   reviews: {len(rows)}')
    st = json.load(open('$STATE'))
    print(f'   checkpointed: {len(st[\"processed_slugs\"])}')
except Exception as e:
    print(f'   error: {e}')
"

    if [ "$chunk" -lt "$NUM_CHUNKS" ]; then
        echo "⏸  Sleeping $SLEEP_BETWEEN seconds..."
        sleep "$SLEEP_BETWEEN"
    fi
done

echo ""
echo "✅ Shard $SHARD_ID done. Send data/reviews_shard$(printf '%02d' $SHARD_ID).csv back to the project owner."
