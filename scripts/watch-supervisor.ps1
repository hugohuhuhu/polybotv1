$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot "runtime-logs"
$localDataRoot = Join-Path $env:LOCALAPPDATA "PolymarketScanner"
$sqlitePath = Join-Path $localDataRoot "polymarket_scanner.db"
$sqliteBackupDir = Join-Path $repoRoot "data"
$stdoutLog = Join-Path $logDir "watch.stdout.log"
$stderrLog = Join-Path $logDir "watch.stderr.log"
$supervisorLog = Join-Path $logDir "watch-supervisor.log"
$watchPidFile = Join-Path $logDir "watch.pid"
$supervisorPidFile = Join-Path $logDir "watch-supervisor.pid"
$mutexName = "Global\PolymarketMispricingWatchSupervisor"

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class SleepControl {
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
"@

$ES_CONTINUOUS = [uint32]2147483648
$ES_SYSTEM_REQUIRED = [uint32]1

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
New-Item -ItemType Directory -Path $localDataRoot -Force | Out-Null
New-Item -ItemType Directory -Path $sqliteBackupDir -Force | Out-Null
Set-Location $repoRoot
$env:SQLITE_PATH = $sqlitePath
$env:SQLITE_BACKUP_DIR = $sqliteBackupDir
$env:SCAN_INTERVAL_SEC = "60"
$env:DISCOVERY_REFRESH_SEC = "900"
$env:DISCOVERY_EVENT_LIMIT = "100"
$env:WATCH_MARKET_LIMIT = "20"
$env:WATCH_BUCKET_GENERAL_LIMIT = "8"
$env:WATCH_BUCKET_EVENT_LIMIT = "4"
$env:WATCH_BUCKET_RECENT_LIMIT = "4"
$env:WATCH_BUCKET_SPECIAL_LIMIT = "2"
$env:BOOK_FETCH_CONCURRENCY = "5"
$env:DASHBOARD_REFRESH_SEC = "30"
$env:MAX_DAILY_LIVE_ORDERS = "42"
$env:AUTO_REDEEM_ENABLED = "true"
$env:AUTO_REDEEM_REFRESH_SEC = "300"
$env:AUTO_REDEEM_MIN_USDCE = "0.01"
$env:NEAR_CLOSE_MAKER_LIVE_ENABLED = "true"
$env:NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE = "0"
$env:NEAR_CLOSE_SCAN_EVENT_LIMIT = "750"
$env:NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES = "75"
$env:NEAR_CLOSE_ORDER_SIZE = "5"
$env:NEAR_CLOSE_MAX_MARKET_EXPOSURE = "5"
$env:NEAR_CLOSE_MAX_TOTAL_EXPOSURE = "25"
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
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK = "0.84"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT = "0.84"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD = "0.05"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE = "0.970"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH = "10"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT = "0.003"

function Write-SupervisorLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $supervisorLog -Value "[$timestamp] $Message"
}

$createdNew = $false
$mutex = $null

try {
    $mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
    if (-not $createdNew) {
        Write-SupervisorLog "duplicate supervisor launch ignored"
        exit 0
    }

    $PID | Set-Content -Path $supervisorPidFile -Encoding ascii

    Register-EngineEvent PowerShell.Exiting -Action {
        [SleepControl]::SetThreadExecutionState($using:ES_CONTINUOUS) | Out-Null
        Remove-Item -Path $using:watchPidFile -ErrorAction SilentlyContinue
        Remove-Item -Path $using:supervisorPidFile -ErrorAction SilentlyContinue
    } | Out-Null

    Write-SupervisorLog "watch supervisor started pid=$PID"
    [SleepControl]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null
    Write-SupervisorLog "sleep prevention enabled"

    while ($true) {
        try {
            [SleepControl]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null
            Write-SupervisorLog "starting child watch process"
            $child = Start-Process -FilePath python `
                -ArgumentList "-m", "app.main", "watch" `
                -WorkingDirectory $repoRoot `
                -RedirectStandardOutput $stdoutLog `
                -RedirectStandardError $stderrLog `
                -PassThru
            $child.Id | Set-Content -Path $watchPidFile -Encoding ascii
            Write-SupervisorLog "child started pid=$($child.Id)"
            $child.WaitForExit()
            Remove-Item -Path $watchPidFile -ErrorAction SilentlyContinue
            Write-SupervisorLog "child exited pid=$($child.Id) code=$($child.ExitCode)"
        } catch {
            Remove-Item -Path $watchPidFile -ErrorAction SilentlyContinue
            Write-SupervisorLog "supervisor caught error: $($_.Exception.Message)"
        }

        Start-Sleep -Seconds 5
    }
} finally {
    [SleepControl]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    if ($mutex -and $createdNew) {
        $mutex.ReleaseMutex() | Out-Null
    }
    if ($mutex) {
        $mutex.Dispose()
    }
}
