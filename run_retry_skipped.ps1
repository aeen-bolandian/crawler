# run_retry_skipped.ps1
# Usage: .\run_retry_skipped.ps1 -ShardId 0 -NumShards 15 -InputSkipped "data/skipped_shard00_retryable.csv"
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [int]$ShardId = 0,

    [Parameter(Position = 1)]
    [int]$NumShards = 15,

    [Parameter(Position = 2)]
    [string]$InputSkipped = "",

    [Parameter()]
    [int]$Chunk = 30,

    [Parameter()]
    [int]$NumChunks = 5,

    [Parameter()]
    [int]$SleepBetween = 900,

    [Parameter()]
    [int]$Workers = 2,

    [Parameter()]
    [string]$ChromePath = ""
)

$ErrorActionPreference = "Stop"

if (-not $InputSkipped) {
    $ShardStr = "{0:D2}" -f $ShardId
    $InputSkipped = "data/skipped_shard$ShardStr.csv"
}

if (-not (Test-Path $InputSkipped)) {
    Write-Error "ERROR: Input skipped file not found: $InputSkipped"
    exit 1
}

# Auto-detect database file
$Db = ""
$TargetShardDb = "cafes_cafe_shard{0:D2}.db" -f $ShardId
if (Test-Path $TargetShardDb) {
    $Db = $TargetShardDb
} elseif (Test-Path "cafes_cafe.db") {
    $Db = "cafes_cafe.db"
} else {
    $ShardDbs = Get-ChildItem -Filter "cafes_cafe_shard*.db" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ShardDbs) {
        $Db = $ShardDbs.Name
    } else {
        Write-Error "ERROR: No database file (*.db) found in project root."
        exit 1
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

$PythonExe = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

$ShardStr = "{0:D2}" -f $ShardId
$Output = "data/reviews_shard$ShardStr.csv"
$State = "crawler_state_shard$ShardStr.json"
$SkippedOut = "data/skipped_shard${ShardStr}_retry.csv"
$UserDataDir = Join-Path $HOME "chrome-debug-profile"

Write-Host "📁 Database: $Db" -ForegroundColor Green
Write-Host "🔢 Shard: $ShardId/$NumShards" -ForegroundColor Green
Write-Host "📄 Retry Input: $InputSkipped" -ForegroundColor Green

for ($chunkIndex = 1; $chunkIndex -le $NumChunks; $chunkIndex++) {
    Write-Host ""
    Write-Host "🔄 Retry Chunk $chunkIndex/$NumChunks — restarting Chrome" -ForegroundColor Cyan

    Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3

    Start-Process -FilePath $ChromePath -ArgumentList @(
        "--remote-debugging-port=9222",
        "--remote-allow-origins=*",
        "--user-data-dir=$UserDataDir",
        "https://www.google.com/maps"
    )
    Start-Sleep -Seconds 5

    try {
        Invoke-RestMethod -Uri "http://localhost:9222/json/new?https://www.google.com/maps" -Method Put -ErrorAction SilentlyContinue | Out-Null
    } catch {}
    Start-Sleep -Seconds 2

    Write-Host "⏳ Waiting for Maps tabs to load (10s)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10

    & $PythonExe run.py `
        --db "$Db" `
        --workers "$Workers" `
        --shard-id "$ShardId" `
        --num-shards "$NumShards" `
        --output "$Output" `
        --state "$State" `
        --skipped "$SkippedOut" `
        --skipped-input "$InputSkipped" `
        --retry-skipped `
        --limit "$Chunk" `
        --log-level INFO

    if ($chunkIndex -lt $NumChunks) {
        Write-Host "⏸  Sleeping $SleepBetween seconds..." -ForegroundColor Gray
        Start-Sleep -Seconds $SleepBetween
    }
}
