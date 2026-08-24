param(
    [string]$FirstOosDate = "2021-01-01",
    [string]$ShortWindows = "10,20,30",
    [string]$LongWindows = "40,60,90",
    [int]$ReferenceShort = 20,
    [int]$ReferenceLong = 60,
    [double]$CostBps = 5.0
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
& $Python -m finance_lab.cli parameter-test-all `
    --first-oos-date $FirstOosDate `
    --shorts $ShortWindows `
    --longs $LongWindows `
    --reference-short $ReferenceShort `
    --reference-long $ReferenceLong `
    --cost-bps $CostBps
Write-Host "Open the *_param_*_report.html files in outputs." -ForegroundColor Cyan
