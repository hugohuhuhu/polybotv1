$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\hug0x\AppData\Local\Programs\Python\Python311\python.exe"
$url = "http://127.0.0.1:8080"
$localDataRoot = Join-Path $env:LOCALAPPDATA "PolymarketScanner"
$sqlitePath = Join-Path $localDataRoot "polymarket_scanner.db"
$sqliteBackupDir = Join-Path $repoRoot "data"
$logDir = Join-Path $repoRoot "runtime-logs"
$serveStdoutLog = Join-Path $logDir "serve.stdout.log"
$serveStderrLog = Join-Path $logDir "serve.stderr.log"
$watchSupervisorScript = Join-Path $PSScriptRoot "watch-supervisor.ps1"
$watchSupervisorStdoutLog = Join-Path $logDir "watch-supervisor.start.stdout.log"
$watchSupervisorStderrLog = Join-Path $logDir "watch-supervisor.start.stderr.log"

function Test-DashboardPortOpen {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $connectTask = $client.ConnectAsync("127.0.0.1", 8080)
        $ready = $connectTask.Wait(500)
        $connected = $ready -and $client.Connected
        $client.Close()
        return $connected
    } catch {
        return $false
    }
}

function Stop-WatchFromPidFiles {
    foreach ($pidFileName in @("watch.pid", "watch-supervisor.pid")) {
        $pidFile = Join-Path $logDir $pidFileName
        if (-not (Test-Path $pidFile)) {
            continue
        }
        $raw = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        if ($raw -match '^\d+$') {
            try {
                Stop-Process -Id ([int]$raw) -Force -ErrorAction SilentlyContinue
            } catch {
            }
        }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path $python)) {
    Write-Host "Python not found:"
    Write-Host $python
    exit 1
}

$entry = Join-Path $repoRoot "app\main.py"
if (-not (Test-Path $entry)) {
    Write-Host "Project entry not found:"
    Write-Host $entry
    exit 1
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
New-Item -ItemType Directory -Path $localDataRoot -Force | Out-Null
New-Item -ItemType Directory -Path $sqliteBackupDir -Force | Out-Null
$env:DASHBOARD_REFRESH_SEC = "30"
$env:SQLITE_PATH = $sqlitePath
$env:SQLITE_BACKUP_DIR = $sqliteBackupDir
$env:AUTO_REDEEM_ENABLED = "true"
$env:AUTO_REDEEM_REFRESH_SEC = "300"
$env:AUTO_REDEEM_MIN_USDCE = "0.01"
$env:AUTO_REDEEM_WRAP_ALLOWANCE_USDCE = "10000"
$env:NEAR_CLOSE_MAKER_LIVE_ENABLED = "true"
$env:NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE = "0"
$env:NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY = "true"
$env:NEAR_CLOSE_MAX_POSITION_SIZE = "10"
$env:NEAR_CLOSE_SCAN_EVENT_LIMIT = "500"
$env:NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES = "75"
$env:NEAR_CLOSE_ORDER_SIZE = "5"
$env:NEAR_CLOSE_MAX_MARKET_EXPOSURE = "5"
$env:NEAR_CLOSE_MAX_TOTAL_EXPOSURE = "25"
$env:NEAR_CLOSE_WEEKEND_MODE_ENABLED = "true"
$env:NEAR_CLOSE_US_MARKET_MODE_ENABLED = "true"
$env:NEAR_CLOSE_US_MARKET_TIMEZONE = "America/New_York"
$env:NEAR_CLOSE_WEEKEND_TIMEZONE = "Asia/Singapore"
$env:NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER = "0.5"
$env:NEAR_CLOSE_WEEKEND_EXPOSURE_MULTIPLIER = "0.7"
$env:NEAR_CLOSE_WEEKEND_SPREAD_MULTIPLIER = "0.8"
$env:NEAR_CLOSE_WEEKEND_START_DISTANCE_MULTIPLIER = "0.75"
$env:NEAR_CLOSE_MAX_MINUTES_TO_END = "15"
$env:NEAR_CLOSE_LIVE_MAX_MINUTES_TO_END = "7"
$env:NEAR_CLOSE_MIN_BEST_ASK = "0.98"
$env:NEAR_CLOSE_MIN_MIDPOINT = "0.975"
$env:NEAR_CLOSE_MAX_SPREAD = "0.025"
$env:NEAR_CLOSE_MIN_NET_EDGE = "0.005"
$env:NEAR_CLOSE_GTD_SECONDS = "1800"
$env:NEAR_CLOSE_REPRICE_THRESHOLD = "0.003"
$env:NEAR_CLOSE_REPRICE_COOLDOWN_SEC = "120"
$env:NEAR_CLOSE_EMERGENCY_SLIPPAGE = "0.03"
$env:NEAR_CLOSE_CRYPTO_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_ORDER_SIZE = "2"
$env:NEAR_CLOSE_CRYPTO_MIN_STRIKE_DISTANCE = "0.02"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE = "5"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END = "0.35"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END = "45"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE = "0.00121"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_START_DISTANCE_LADDER = "7:0.0024,6:0.0018,5:0.00121,3.5:0.0010,1.5:0.00085,0.35:0.00085"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_CANCEL_START_DISTANCE = "0.00055"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK = "0.84"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT = "0.84"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD = "0.05"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE = "0.970"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_SKIP_BID_AT_OR_ABOVE = "0.96"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH = "10"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT = "0.003"
$env:NEAR_CLOSE_PROFIT_TAKE_ENABLED = "true"
$env:NEAR_CLOSE_PROFIT_TAKE_LIVE_ENABLED = "true"
$env:NEAR_CLOSE_PROFIT_TAKE_SHADOW_ENABLED = "true"
$env:NEAR_CLOSE_PROFIT_TAKE_ORDER_TYPE = "GTD"
$env:NEAR_CLOSE_PROFIT_TAKE_GTD_SECONDS = "240"
$env:NEAR_CLOSE_PROFIT_TAKE_LADDER = "0.865:0.950,0.885:0.955,0.905:0.965,0.925:0.970,0.940:0.985"
$env:SCAN_INTERVAL_SEC = "30"
$env:WATCH_SCAN_TIMEOUT_SEC = "60"
$env:WATCH_TIMEOUT_RETRY_SEC = "30"

Write-Host "[1/3] Starting dashboard..."
Stop-WatchFromPidFiles
$cleanupScript = Join-Path $PSScriptRoot "cleanup-near-close-orders.ps1"
if (Test-Path $cleanupScript) {
    & $cleanupScript | Out-Null
}
if (-not (Test-DashboardPortOpen)) {
    Start-Process -FilePath $python `
        -ArgumentList "-m", "app.main", "serve" `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $serveStdoutLog `
        -RedirectStandardError $serveStderrLog | Out-Null
}

Write-Host "[2/3] Starting watch supervisor..."
if (Test-Path $watchSupervisorScript) {
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$watchSupervisorScript`"" `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $watchSupervisorStdoutLog `
        -RedirectStandardError $watchSupervisorStderrLog | Out-Null
}

Write-Host "[3/3] Opening browser..."
Start-Sleep -Milliseconds 800
Start-Process $url
Write-Host "Done. If the dashboard is still loading, refresh the browser in a few seconds."
exit 0
