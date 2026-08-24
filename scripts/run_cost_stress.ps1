param(
    [string]$FirstOosDate = "2021-01-01",
    [string]$Costs = "5,10,20,50"
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
& $Python -m finance_lab.cli cost-stress-all `
    --first-oos-date $FirstOosDate `
    --costs $Costs
Write-Host "Open the *_cost_*_report.html files in outputs." -ForegroundColor Cyan
