$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
}
Set-Location $ProjectRoot
& $Python -m ruff check .
if ($LASTEXITCODE -ne 0) {
    throw "Ruff failed"
}
& $Python -m mypy src
if ($LASTEXITCODE -ne 0) {
    throw "mypy failed"
}
& $Python -m pytest --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80
if ($LASTEXITCODE -ne 0) {
    throw "pytest failed"
}
& $Python -m compileall -q src tests
if ($LASTEXITCODE -ne 0) {
    throw "compileall failed"
}
& $Python scripts/check_markdown_links.py
if ($LASTEXITCODE -ne 0) {
    throw "Markdown link check failed"
}
& $Python -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "pip check failed"
}
Write-Host "All local verification checks passed." -ForegroundColor Green
