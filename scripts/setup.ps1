$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $Created = $false
    try {
        & py -3.12 -m venv .venv
        $Created = $LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $VenvPython)
    } catch {
        $Created = $false
    }

    if (-not $Created) {
        $VersionText = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -eq 0 -and $VersionText -in @("3.11", "3.12", "3.13")) {
            & python -m venv .venv
            $Created = $LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $VenvPython)
        }
    }

    if (-not $Created) {
        throw "Python 3.11-3.13 was not found. Install Python 3.12 and run again."
    }
}

& $VenvPython -m pip install pip==26.2.1 setuptools==80.9.0 wheel==0.45.1
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the pinned Python build toolchain"
}
& $VenvPython -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the pinned project dependencies"
}
& $VenvPython -m pip install --no-build-isolation -e . --no-deps
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install finance-lab"
}
Write-Host "Environment setup completed." -ForegroundColor Green
