param(
    [string]$FirstOosDate = "2021-01-01",
    [int]$FoldMonths = 12
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
& $Python -m finance_lab.cli walk-forward-all `
    --first-oos-date $FirstOosDate `
    --fold-months $FoldMonths
Write-Host "Open the *_walk_*_report.html files in outputs." -ForegroundColor Cyan
