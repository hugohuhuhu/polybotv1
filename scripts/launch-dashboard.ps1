$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\hug0x\AppData\Local\Programs\Python\Python311\python.exe"
$url = "http://127.0.0.1:8080"
$sqlitePath = "C:\Users\hug0x\Desktop\polymarket-scanner-data\polymarket_scanner.db"
$logDir = Join-Path $repoRoot "runtime-logs"
$serveStdoutLog = Join-Path $logDir "serve.stdout.log"
$serveStderrLog = Join-Path $logDir "serve.stderr.log"

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
New-Item -ItemType Directory -Path (Split-Path -Parent $sqlitePath) -Force | Out-Null
$env:DASHBOARD_REFRESH_SEC = "30"
$env:SQLITE_PATH = $sqlitePath
$env:MAX_DAILY_LIVE_ORDERS = "21"
$env:AUTO_REDEEM_ENABLED = "true"
$env:AUTO_REDEEM_REFRESH_SEC = "300"
$env:AUTO_REDEEM_MIN_USDCE = "0.01"
$env:NEAR_CLOSE_MAKER_LIVE_ENABLED = "true"
$env:NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE = "0"
$env:NEAR_CLOSE_SCAN_EVENT_LIMIT = "750"
$env:NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES = "75"
$env:NEAR_CLOSE_ORDER_SIZE = "5"
$env:NEAR_CLOSE_MAX_MARKET_EXPOSURE = "5"
$env:NEAR_CLOSE_MAX_TOTAL_EXPOSURE = "15"
$env:NEAR_CLOSE_MAX_MINUTES_TO_END = "15"
$env:NEAR_CLOSE_MIN_BEST_ASK = "0.98"
$env:NEAR_CLOSE_MIN_MIDPOINT = "0.975"
$env:NEAR_CLOSE_MAX_SPREAD = "0.025"
$env:NEAR_CLOSE_MIN_NET_EDGE = "0.005"
$env:NEAR_CLOSE_GTD_SECONDS = "1800"
$env:NEAR_CLOSE_REPRICE_THRESHOLD = "0.003"
$env:NEAR_CLOSE_REPRICE_COOLDOWN_SEC = "120"
$env:NEAR_CLOSE_CRYPTO_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_ORDER_SIZE = "2"
$env:NEAR_CLOSE_CRYPTO_MIN_STRIKE_DISTANCE = "0.02"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE = "5"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END = "1.5"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END = "45"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE = "0.003"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_CANCEL_START_DISTANCE = "0.002"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK = "0.75"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT = "0.60"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD = "0.04"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE = "0.988"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH = "10"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT = "0.003"

Write-Host "[1/2] Starting dashboard..."
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

Write-Host "[2/2] Opening browser..."
Start-Sleep -Milliseconds 800
Start-Process $url
Write-Host "Done. If the dashboard is still loading, refresh the browser in a few seconds."
exit 0
