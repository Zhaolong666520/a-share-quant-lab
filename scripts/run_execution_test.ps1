param(
    [string]$FirstOosDate = "2021-01-01",
    [int]$ShortWindow = 20,
    [int]$LongWindow = 60,
    [double]$CostBps = 5.0,
    [int]$DelayDays = 1,
    [double]$LockThreshold = 0.095
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
& $Python -m finance_lab.cli execution-test-all `
    --first-oos-date $FirstOosDate `
    --short $ShortWindow `
    --long $LongWindow `
    --cost-bps $CostBps `
    --delay-days $DelayDays `
    --lock-threshold $LockThreshold
Write-Host "Open the *_exec_*_report.html files in outputs." -ForegroundColor Cyan
