param(
    [string]$AsOfDate = (Get-Date -Format "yyyy-MM-dd"),
    [int]$StaleAfterBusinessDays = 3
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
& $Python -m finance_lab.cli manifest `
    --as-of $AsOfDate `
    --stale-after-business-days $StaleAfterBusinessDays
if ($LASTEXITCODE -ne 0) {
    throw "Data health command failed with exit code $LASTEXITCODE"
}
Write-Host "Open dataset_manifest_*_report.html in outputs." -ForegroundColor Cyan
