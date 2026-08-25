param(
    [string]$SplitDate = "2023-01-01",
    [int]$ShortWindow = 20,
    [int]$LongWindow = 60,
    [int]$MomentumLookback = 120,
    [double]$CostBps = 5.0
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
Set-Location $ProjectRoot
& $Python -m finance_lab.cli compare-all `
    --split-date $SplitDate `
    --short $ShortWindow `
    --long $LongWindow `
    --momentum-lookback $MomentumLookback `
    --cost-bps $CostBps
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Write-Host "Open the *_strategy_compare_*_report.html files in outputs." -ForegroundColor Cyan
