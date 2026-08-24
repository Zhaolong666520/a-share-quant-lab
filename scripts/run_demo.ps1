$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
}
Set-Location $ProjectRoot
& $Python -m finance_lab.cli demo
Write-Host "Open outputs\demo_000300_sma_report.html." -ForegroundColor Cyan
