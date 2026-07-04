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
$watchLivenessFile = Join-Path $logDir "watch.liveness"
$supervisorPidFile = Join-Path $logDir "watch-supervisor.pid"
$mutexName = "Global\PolymarketMispricingWatchSupervisor"
$childStaleKillSec = 45
$childStartupGraceSec = 10
$childStartupLivenessGraceSec = 90
$restartDelaySec = 5

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

function Clear-InvalidPrivateKeyOverride {
    $raw = [Environment]::GetEnvironmentVariable("POLYMARKET_PRIVATE_KEY", "Process")
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return
    }
    $normalized = $raw.Trim()
    $hex = $normalized
    if ($normalized.StartsWith("0x", [System.StringComparison]::OrdinalIgnoreCase)) {
        $hex = $normalized.Substring(2)
    }
    if (($hex.Length -ne 64) -or ($hex -notmatch '^[0-9a-fA-F]{64}$')) {
        Remove-Item Env:\POLYMARKET_PRIVATE_KEY -ErrorAction SilentlyContinue
        Write-SupervisorLog "Ignoring invalid inherited POLYMARKET_PRIVATE_KEY; .env will be used if configured."
    }
}

function Write-SupervisorLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $supervisorLog -Value "[$timestamp] $Message"
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
New-Item -ItemType Directory -Path $localDataRoot -Force | Out-Null
New-Item -ItemType Directory -Path $sqliteBackupDir -Force | Out-Null
Set-Location $repoRoot
Clear-InvalidPrivateKeyOverride
$env:SQLITE_PATH = $sqlitePath
$env:SQLITE_BACKUP_DIR = $sqliteBackupDir
$env:SCAN_INTERVAL_SEC = "8"
$env:WATCH_SCAN_TIMEOUT_SEC = "10"
$env:WATCH_TIMEOUT_RETRY_SEC = "21"
$env:WATCH_LIVE_FILL_SYNC_ENABLED = "false"
$childStaleKillSec = [math]::Max(
    $childStaleKillSec,
    [int]$env:WATCH_SCAN_TIMEOUT_SEC + [int]$env:WATCH_TIMEOUT_RETRY_SEC + 15
)
$env:DISCOVERY_REFRESH_SEC = "900"
$env:DISCOVERY_EVENT_LIMIT = "100"
$env:WATCH_MARKET_LIMIT = "4"
$env:WATCH_BUCKET_GENERAL_LIMIT = "8"
$env:WATCH_BUCKET_EVENT_LIMIT = "4"
$env:WATCH_BUCKET_RECENT_LIMIT = "4"
$env:WATCH_BUCKET_SPECIAL_LIMIT = "2"
$env:BOOK_FETCH_CONCURRENCY = "12"
$env:GAMMA_TIMEOUT_SEC = "2"
$env:GAMMA_RETRIES = "1"
$env:CRYPTO_PRICE_TIMEOUT_SEC = "1"
$env:BOOK_FETCH_TIMEOUT_SEC = "1.5"
$env:BOOK_FETCH_RETRIES = "1"
$env:DASHBOARD_REFRESH_SEC = "30"
$env:AUTO_REDEEM_ENABLED = "true"
$env:AUTO_REDEEM_REFRESH_SEC = "300"
$env:AUTO_REDEEM_MIN_USDCE = "0.01"
$env:AUTO_REDEEM_WRAP_ALLOWANCE_USDCE = "10000"
$env:NEAR_CLOSE_MAKER_LIVE_ENABLED = "true"
$env:NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE = "0"
$env:NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY = "true"
$env:NEAR_CLOSE_OPEN_POSITION_MONITOR_SEC = "2"
$env:NEAR_CLOSE_MAX_POSITION_SIZE = "10"
$env:NEAR_CLOSE_SCAN_EVENT_LIMIT = "500"
$env:NEAR_CLOSE_SCAN_POOL_LIMIT = "4"
$env:NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES = "75"
$env:NEAR_CLOSE_ORDER_SIZE = "5"
$env:NEAR_CLOSE_MAX_MARKET_EXPOSURE = "5"
$env:NEAR_CLOSE_MAX_TOTAL_EXPOSURE = "25"
$env:MAX_DAILY_LIVE_NOTIONAL = "0"
$env:NEAR_CLOSE_WEEKEND_MODE_ENABLED = "true"
$env:NEAR_CLOSE_WEEKEND_MODE_FORCE = "true"
$env:NEAR_CLOSE_US_MARKET_MODE_ENABLED = "true"
$env:NEAR_CLOSE_US_MARKET_TIMEZONE = "America/New_York"
$env:NEAR_CLOSE_WEEKEND_TIMEZONE = "Asia/Singapore"
$env:NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER = "0.5"
$env:NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_ORDER_SIZE = "5"
$env:NEAR_CLOSE_WEEKEND_EXPOSURE_MULTIPLIER = "0.7"
$env:NEAR_CLOSE_WEEKEND_SPREAD_MULTIPLIER = "1.2"
$env:NEAR_CLOSE_WEEKEND_START_DISTANCE_MULTIPLIER = "0.15"
$env:NEAR_CLOSE_MAX_MINUTES_TO_END = "15"
$env:NEAR_CLOSE_LIVE_MAX_MINUTES_TO_END = "7"
$env:NEAR_CLOSE_ENTRY_MAX_SECONDS = "60"
$env:NEAR_CLOSE_ENTRY_MIN_SECONDS = "30"
$env:NEAR_CLOSE_EXISTING_ORDER_HARD_CANCEL_SECONDS = "12"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_PREWARM_SECONDS = "60"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_FAST_SCAN_SEC = "2"
$env:NEAR_CLOSE_FINAL_SECONDS_ALLOW_ENTRY = "true"
$env:NEAR_CLOSE_LOG_ENTRY_TELEMETRY = "true"
$env:NEAR_CLOSE_MIN_BEST_ASK = "0.98"
$env:NEAR_CLOSE_MIN_MIDPOINT = "0.975"
$env:NEAR_CLOSE_MAX_SPREAD = "0.025"
$env:NEAR_CLOSE_MIN_NET_EDGE = "0.005"
$env:NEAR_CLOSE_GTD_SECONDS = "1800"
$env:NEAR_CLOSE_REPRICE_THRESHOLD = "0.003"
$env:NEAR_CLOSE_REPRICE_COOLDOWN_SEC = "120"
$env:NEAR_CLOSE_HARD_STOP_OFFSET = "0.2"
$env:NEAR_CLOSE_STOP_EXIT_MAX_SPREAD = "0.08"
$env:NEAR_CLOSE_STOP_EXIT_SETTLEMENT_GRACE_SEC = "90"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_STOP_REQUIRES_DIRECTION_BREAK = "true"
$env:NEAR_CLOSE_EMERGENCY_SLIPPAGE = "0.03"
$env:NEAR_CLOSE_CRYPTO_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_ORDER_SIZE = "2"
$env:NEAR_CLOSE_CRYPTO_MIN_STRIKE_DISTANCE = "0.02"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE = "5"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END = "1.5"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_NO_NEW_ENTRY_LAST_SECONDS = "0"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END = "45"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE = "0.00121"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_START_DISTANCE_LADDER = "7:0.0024,6:0.0018,5:0.00121,3.5:0.0010,1.5:0.00085,0.35:0.00085"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_CANCEL_START_DISTANCE = "0.00005"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_RESOLUTION_BUCKET_MAX_LIVE_ORDERS = "1"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_WRONG_RESOLUTION_COOLDOWN_BUCKETS = "1"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_STOP_DIRECTION_BREAK_BUFFER = "0.00075"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK = "0.84"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT = "0.84"
$env:NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_BEST_ASK = "0.78"
$env:NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_MIDPOINT = "0.76"
$env:NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_ENTRY_PRICE = "0.86"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD = "0.05"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE = "0.970"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE = "0.86"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MAX_ENTRY_PRICE = "0.90"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_SKIP_BID_AT_OR_ABOVE = "0.96"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH = "18"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT = "0.003"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_ENABLED = "true"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MIN_SECONDS = "30"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MAX_SECONDS = "45"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MAX_PRICE = "0.90"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MAX_SPREAD = "0.02"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MIN_ASK_DEPTH = "5"
$env:NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MIN_START_DISTANCE_RATIO = "2"
$env:NEAR_CLOSE_PROFIT_TAKE_ENABLED = "true"
$env:NEAR_CLOSE_PROFIT_TAKE_LIVE_ENABLED = "true"
$env:NEAR_CLOSE_PROFIT_TAKE_SHADOW_ENABLED = "true"
$env:NEAR_CLOSE_PROFIT_TAKE_ORDER_TYPE = "GTD"
$env:NEAR_CLOSE_PROFIT_TAKE_GTD_SECONDS = "240"
$env:NEAR_CLOSE_PROFIT_TAKE_LADDER = "0.865:0.950,0.885:0.955,0.905:0.965,0.925:0.970,0.940:0.985"
$env:NEAR_CLOSE_SECOND_CHANCE_EXIT_ENABLED = "false"

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
            Remove-Item -Path $watchLivenessFile -ErrorAction SilentlyContinue
            Write-SupervisorLog "starting child watch process"
            $child = Start-Process -FilePath python `
                -ArgumentList "-m", "app.main", "watch" `
                -WorkingDirectory $repoRoot `
                -WindowStyle Hidden `
                -RedirectStandardOutput $stdoutLog `
                -RedirectStandardError $stderrLog `
                -PassThru
            $childStartedAt = Get-Date
            $child.Id | Set-Content -Path $watchPidFile -Encoding ascii
            Write-SupervisorLog "child started pid=$($child.Id)"
            while (-not $child.HasExited) {
                Start-Sleep -Seconds 5
                $childAge = ((Get-Date) - $childStartedAt).TotalSeconds
                if (Test-Path $watchLivenessFile) {
                    $age = ((Get-Date) - (Get-Item $watchLivenessFile).LastWriteTime).TotalSeconds
                    if (($childAge -gt $childStartupGraceSec) -and ($age -gt $childStaleKillSec)) {
                        Write-SupervisorLog "child stale for $([math]::Round($age, 1))s; killing pid=$($child.Id)"
                        Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
                        break
                    }
                } elseif ($childAge -gt $childStartupLivenessGraceSec) {
                    Write-SupervisorLog "child produced no liveness for $([math]::Round($childAge, 1))s; killing pid=$($child.Id)"
                    Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
                    break
                }
            }
            $child.WaitForExit()
            Remove-Item -Path $watchPidFile -ErrorAction SilentlyContinue
            Remove-Item -Path $watchLivenessFile -ErrorAction SilentlyContinue
            Write-SupervisorLog "child exited pid=$($child.Id) code=$($child.ExitCode)"
        } catch {
            Remove-Item -Path $watchPidFile -ErrorAction SilentlyContinue
            Remove-Item -Path $watchLivenessFile -ErrorAction SilentlyContinue
            Write-SupervisorLog "supervisor caught error: $($_.Exception.Message)"
        }

        Start-Sleep -Seconds $restartDelaySec
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
