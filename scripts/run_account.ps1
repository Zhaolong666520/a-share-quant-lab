param(
    [int]$ShortWindow = 20,
    [int]$LongWindow = 60,
    [double]$InitialCash = 100000.0,
    [int]$LotSize = 100,
    [double]$CommissionBps = 3.0,
    [double]$MinimumCommission = 5.0,
    [double]$SellTaxBps = 0.0,
    [double]$SlippageBps = 2.0
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
}
Set-Location $ProjectRoot
& $Python -m finance_lab.cli account-all `
    --short $ShortWindow `
    --long $LongWindow `
    --initial-cash $InitialCash `
    --lot-size $LotSize `
    --commission-bps $CommissionBps `
    --minimum-commission $MinimumCommission `
    --sell-tax-bps $SellTaxBps `
    --slippage-bps $SlippageBps
if ($LASTEXITCODE -ne 0) {
    throw "Account ledger command failed with exit code $LASTEXITCODE"
}
Write-Host "Open the *_account_*_report.html files in outputs." -ForegroundColor Cyan
