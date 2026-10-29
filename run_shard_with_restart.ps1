# run_shard_with_restart.ps1
# Usage: .\run_shard_with_restart.ps1 -ShardId 0 -NumShards 15
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [int]$ShardId = 0,

    [Parameter(Position = 1)]
    [int]$NumShards = 15,

    [Parameter()]
    [int]$Chunk = 50,

    [Parameter()]
    [int]$NumChunks = 10,

    [Parameter()]
    [int]$SleepBetween = 600,

    [Parameter()]
    [int]$Workers = 2,

    [Parameter()]
    [string]$ChromePath = ""
)

$ErrorActionPreference = "Stop"

# Auto-detect database file
$Db = ""
$TargetShardDb = "cafes_cafe_shard{0:D2}.db" -f $ShardId
if (Test-Path $TargetShardDb) {
    $Db = $TargetShardDb
    Write-Host "Auto-detected shard database: $Db" -ForegroundColor Cyan
} elseif (Test-Path "cafes_cafe.db") {
    $Db = "cafes_cafe.db"
    Write-Host "Auto-detected database: $Db" -ForegroundColor Cyan
} else {
    $ShardDbs = Get-ChildItem -Filter "cafes_cafe_shard*.db" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ShardDbs) {
        $Db = $ShardDbs.Name
        Write-Host "Auto-detected database: $Db" -ForegroundColor Cyan
    } else {
        $AnyDb = Get-ChildItem -Filter "*.db" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($AnyDb) {
            $Db = $AnyDb.Name
            Write-Host "Using database (auto-detected): $Db" -ForegroundColor Yellow
        } else {
            Write-Error "ERROR: No database file (*.db) found in project root."
            exit 1
        }
    }
}

# Auto-detect Chrome executable
if (-not $ChromePath) {
    $CandidatePaths = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($p in $CandidatePaths) {
        if (Test-Path $p) {
            $ChromePath = $p
            break
        }
    }
}

if (-not $ChromePath -or -not (Test-Path $ChromePath)) {
    Write-Error "ERROR: Google Chrome executable not found. Please provide -ChromePath."
    exit 1
}

# Python executable
$PythonExe = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

$ShardStr = "{0:D2}" -f $ShardId
$Output = "data/reviews_shard$ShardStr.csv"
$State = "crawler_state_shard$ShardStr.json"
$Skipped = "data/skipped_shard$ShardStr.csv"
$UserDataDir = Join-Path $HOME "chrome-debug-profile"

Write-Host "📁 Database: $Db" -ForegroundColor Green
Write-Host "🔢 Shard: $ShardId/$NumShards" -ForegroundColor Green
Write-Host "⚙️  Workers: $Workers" -ForegroundColor Green
Write-Host "⏸  Sleep between chunks: ${SleepBetween}s" -ForegroundColor Green
Write-Host "🌐 Chrome: $ChromePath" -ForegroundColor Green

for ($chunkIndex = 1; $chunkIndex -le $NumChunks; $chunkIndex++) {
    Write-Host ""
    Write-Host "════════════════════════════════════════════════════" -ForegroundColor DarkCyan
    Write-Host "🔄 Chunk $chunkIndex/$NumChunks — restarting Chrome" -ForegroundColor Cyan
    Write-Host "════════════════════════════════════════════════════" -ForegroundColor DarkCyan

    # Kill existing Chrome instances cleanly to release memory
    Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3

    # Launch Chrome with remote debugging
    Start-Process -FilePath $ChromePath -ArgumentList @(
        "--remote-debugging-port=9222",
        "--remote-allow-origins=*",
        "--user-data-dir=$UserDataDir",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.google.com/maps"
    )
    Start-Sleep -Seconds 5

    # Open extra Google Maps tab for worker pooling
    try {
        Invoke-RestMethod -Uri "http://localhost:9222/json/new?https://www.google.com/maps" -Method Put -ErrorAction SilentlyContinue | Out-Null
    } catch {
        # Fallback if endpoint returns unexpected code
    }
    Start-Sleep -Seconds 2

    Write-Host "⏳ Waiting for Maps tabs to load (10s)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10

    # Execute crawler chunk
    & $PythonExe run.py `
        --db "$Db" `
        --workers "$Workers" `
        --shard-id "$ShardId" `
        --num-shards "$NumShards" `
        --output "$Output" `
        --state "$State" `
        --skipped "$Skipped" `
        --limit "$Chunk" `
        --log-level INFO

    $ExitCode = $LASTEXITCODE

    Write-Host ""
    Write-Host "📊 After chunk ${chunkIndex}:" -ForegroundColor Green
    & $PythonExe -c "
import csv, json, sys
try:
    with open('$Output', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
        slugs = {r['cafe_slug'] for r in rows}
    print(f'   cafes: {len(slugs)}')
    print(f'   reviews: {len(rows)}')
    st = json.load(open('$State', encoding='utf-8'))
    print(f'   checkpointed: {len(st[\"processed_slugs\"])}')
except Exception as e:
    print(f'   stats error: {e}')
"

    if ($ExitCode -ne 0) {
        Write-Host "⚠ Crawler exited with non-zero code ($ExitCode)." -ForegroundColor Yellow
    }

    if ($chunkIndex -lt $NumChunks) {
        Write-Host "⏸  Sleeping $SleepBetween seconds..." -ForegroundColor Gray
        Start-Sleep -Seconds $SleepBetween
    }
}

Write-Host ""
Write-Host "✅ Shard $ShardId done. Output saved to $Output" -ForegroundColor Green
