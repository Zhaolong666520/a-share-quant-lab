param(
    [string]$AsOfDate = (Get-Date -Format "yyyy-MM-dd"),
    [string]$StartDate = "2018-01-01",
    [int]$StaleAfterBusinessDays = 3,
    [string]$Portfolio = "default"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Environment setup failed with exit code $LASTEXITCODE"
    }
}
Set-Location $ProjectRoot

& $Python -m finance_lab.cli fetch --start $StartDate --end $AsOfDate
if ($LASTEXITCODE -ne 0) { throw "Data fetch failed with exit code $LASTEXITCODE" }

& $Python -m finance_lab.cli manifest --as-of $AsOfDate --stale-after-business-days $StaleAfterBusinessDays
if ($LASTEXITCODE -ne 0) { throw "Data manifest failed with exit code $LASTEXITCODE" }

& $Python -m finance_lab.cli paper-run --portfolio $Portfolio --as-of $AsOfDate --stale-after-business-days $StaleAfterBusinessDays
if ($LASTEXITCODE -eq 4) {
    & $Python -m finance_lab.cli paper-init --portfolio $Portfolio --as-of $AsOfDate --stale-after-business-days $StaleAfterBusinessDays
    if ($LASTEXITCODE -ne 0) { throw "Paper initialization failed with exit code $LASTEXITCODE" }
} elseif ($LASTEXITCODE -ne 0) {
    throw "Paper run failed with exit code $LASTEXITCODE"
}

& $Python -m finance_lab.cli paper-status --portfolio $Portfolio
if ($LASTEXITCODE -ne 0) { throw "Paper status failed with exit code $LASTEXITCODE" }

$LatestReport = Join-Path $ProjectRoot "outputs\$Portfolio`_paper_latest_report.html"
if (-not (Test-Path -LiteralPath $LatestReport)) { throw "Paper report was not created: $LatestReport" }
Start-Process -FilePath $LatestReport
