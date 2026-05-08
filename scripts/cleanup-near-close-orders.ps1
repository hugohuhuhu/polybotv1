$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$sqlitePath = "C:\Users\hug0x\Desktop\polymarket-scanner-data\polymarket_scanner.db"
$python = "C:\Users\hug0x\AppData\Local\Programs\Python\Python311\python.exe"

$env:SQLITE_PATH = $sqlitePath

$cleanupCode = @'
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository

order_ids = [
    "0xc1da8bbd13c5cc0512d3877d1db0f5af7ae55db5dd91b8c6a5e3dca7dfa0fe65",
    "0x44369146b5faadb39f7ed294e2d55cff3b44e416d6ca9dd7f1502d88a239cbd5",
    "0xb24c00a0423b60070395be6ddaf24da1e208c12290efbcac2c1f2cb64e47e932",
]
repo = ScannerRepository(connect_db(r"C:\Users\hug0x\Desktop\polymarket-scanner-data\polymarket_scanner.db"))
updated = repo.mark_live_orders_cancelled(order_ids, status="expired")
print(f"near-close cleanup marked {updated} old orders expired")
'@

$cleanupCode | & $python -
