param(
    [ValidatePattern("^v[0-9]+(?:\.[0-9]+){0,2}$")]
    [string]$Version = "v0.9.0"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DeliverablesRoot = (Resolve-Path (Join-Path $ProjectRoot "outputs")).Path
$StageLeaf = "package_staging_$Version"
$VerifyLeaf = "package_verify_$Version"
$StagePath = Join-Path $ProjectRoot $StageLeaf
$VerifyPath = Join-Path $ProjectRoot $VerifyLeaf
$ArchivePath = Join-Path $DeliverablesRoot "a-share-quant-lab-source-$Version.zip"
$ProjectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $ProjectPython)) {
    throw "Run scripts\setup.ps1 before packaging"
}

function Assert-SafeTemporaryPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedLeaf
    )

    $FullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $RootPrefix = $ProjectRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $FullPath.StartsWith($RootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $FullPath"
    }
    if ([System.IO.Path]::GetFileName($FullPath) -ne $ExpectedLeaf) {
        throw "Unexpected temporary directory: $FullPath"
    }
    return $FullPath
}

function Remove-SafeTemporaryDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedLeaf
    )

    $SafePath = Assert-SafeTemporaryPath -Path $Path -ExpectedLeaf $ExpectedLeaf
    if (Test-Path -LiteralPath $SafePath) {
        Remove-Item -LiteralPath $SafePath -Recurse -Force
    }
}

$RootFiles = @(
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "pyproject.toml",
    "requirements-lock.txt",
    "README.md",
    "SECURITY.md",
    "run_account.cmd",
    "run_cost_stress.cmd",
    "run_data_health.cmd",
    "run_demo.cmd",
    "run_demo.sh",
    "run_execution_test.cmd",
    "run_experiment.cmd",
    "run_parameter_test.cmd",
    "run_paper_daily.cmd",
    "run_paper_trading.cmd",
    "run_strategy_compare.cmd",
    "run_walk_forward.cmd",
    "update_data.cmd",
    "install_paper_daily_task.cmd",
    "uninstall_paper_daily_task.cmd",
    "verify.cmd"
)
$SourceDirectories = @(".github", "config", "docs", "experiments", "research", "scripts", "src", "tests")
$FourthLessonName = ([char]0x7B2C).ToString() + ([char]0x56DB).ToString() + ([char]0x8BFE).ToString() + ".md"
$FifthLessonName = ([char]0x7B2C).ToString() + ([char]0x4E94).ToString() + ([char]0x8BFE).ToString() + ".md"
$SixthLessonName = ([char]0x7B2C).ToString() + ([char]0x516D).ToString() + ([char]0x8BFE).ToString() + ".md"
$SeventhLessonName = ([char]0x7B2C).ToString() + ([char]0x4E03).ToString() + ([char]0x8BFE).ToString() + ".md"
$EighthLessonName = ([char]0x7B2C).ToString() + ([char]0x516B).ToString() + ([char]0x8BFE).ToString() + ".md"
$NinthLessonName = ([char]0x7B2C).ToString() + ([char]0x4E5D).ToString() + ([char]0x8BFE).ToString() + ".md"
$RequiredArchivePaths = @(
    ".gitattributes",
    (Join-Path "docs" $FourthLessonName),
    (Join-Path "docs" $FifthLessonName),
    (Join-Path "docs" $SixthLessonName),
    (Join-Path "docs" $SeventhLessonName),
    (Join-Path "docs" $EighthLessonName),
    (Join-Path "docs" $NinthLessonName),
    "experiments\004_parameter_sensitivity.md",
    "experiments\005_execution_feasibility.md",
    "experiments\006_data_and_account_ledger.md",
    "experiments\007_strategy_comparison.md",
    "experiments\008_forward_paper_trading.md",
    "requirements-lock.txt",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    ".github\workflows\ci.yml",
    ".github\ISSUE_TEMPLATE\research_review.yml",
    "docs\quickstart.en.md",
    "docs\assets\social-preview.jpg",
    "docs\assets\account-equity.png",
    "docs\assets\demo-equity.png",
    "docs\releases\v0.8.0.md",
    "docs\releases\v0.9.0.md",
    "src\finance_lab\cost_sensitivity.py",
    "src\finance_lab\parameter_sensitivity.py",
    "src\finance_lab\parameter_sensitivity_report.py",
    "src\finance_lab\execution_feasibility.py",
    "src\finance_lab\execution_feasibility_report.py",
    "src\finance_lab\data_manifest.py",
    "src\finance_lab\trading_calendar.py",
    "src\finance_lab\ledger.py",
    "src\finance_lab\ledger_report.py",
    "src\finance_lab\strategy_comparison.py",
    "src\finance_lab\strategy_comparison_report.py",
    "src\finance_lab\paper_models.py",
    "src\finance_lab\paper_attribution.py",
    "src\finance_lab\paper_automation.py",
    "src\finance_lab\paper_benchmark.py",
    "src\finance_lab\paper_observation.py",
    "src\finance_lab\paper_risk.py",
    "src\finance_lab\cash_distributions.py",
    "src\finance_lab\share_adjustments.py",
    "src\finance_lab\paper_engine.py",
    "src\finance_lab\paper_lock.py",
    "src\finance_lab\paper_store.py",
    "src\finance_lab\paper_pipeline.py",
    "src\finance_lab\paper_report.py",
    "src\finance_lab\cli.py",
    "src\finance_lab\pipeline.py",
    "tests\test_cost_sensitivity.py",
    "tests\test_parameter_sensitivity.py",
    "tests\test_execution_feasibility.py",
    "tests\test_data_manifest.py",
    "tests\test_trading_calendar.py",
    "tests\test_ledger.py",
    "tests\test_strategy_comparison.py",
    "tests\test_paper_models.py",
    "tests\test_paper_attribution.py",
    "tests\test_paper_automation.py",
    "tests\test_paper_benchmark.py",
    "tests\test_paper_observation.py",
    "tests\test_paper_risk.py",
    "tests\test_cash_distributions.py",
    "tests\test_share_adjustments.py",
    "tests\test_paper_engine.py",
    "tests\test_paper_lock.py",
    "tests\test_paper_store.py",
    "tests\test_paper_pipeline.py",
    "tests\test_paper_report.py",
    "tests\test_paper_cli.py",
    "tests\__init__.py",
    "tests\paper_helpers.py",
    "scripts\package_release.ps1",
    "scripts\setup.sh",
    "scripts\run_demo.sh",
    "scripts\run_parameter_test.ps1",
    "scripts\run_execution_test.ps1",
    "scripts\run_data_health.ps1",
    "scripts\run_account.ps1",
    "scripts\run_strategy_compare.ps1",
    "scripts\run_paper_daily.ps1",
    "scripts\install_paper_daily_task.ps1",
    "scripts\run_paper_trading.ps1",
    "run_cost_stress.cmd",
    "run_parameter_test.cmd",
    "run_execution_test.cmd",
    "run_data_health.cmd",
    "run_account.cmd",
    "run_strategy_compare.cmd",
    "run_paper_daily.cmd",
    "install_paper_daily_task.cmd",
    "uninstall_paper_daily_task.cmd",
    "run_paper_trading.cmd",
    "run_demo.sh",
    "config\sse_trading_calendar.json",
    "config\cash_distribution_template.csv",
    "config\share_adjustment_template.csv",
    "data\.gitkeep",
    "outputs\.gitkeep"
)

try {
    Remove-SafeTemporaryDirectory -Path $StagePath -ExpectedLeaf $StageLeaf
    Remove-SafeTemporaryDirectory -Path $VerifyPath -ExpectedLeaf $VerifyLeaf
    New-Item -ItemType Directory -Path $StagePath | Out-Null

    foreach ($File in $RootFiles) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $File) -Destination $StagePath
    }
    foreach ($Directory in $SourceDirectories) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $Directory) -Destination $StagePath -Recurse
    }

    foreach ($EmptyDirectory in @("data", "outputs")) {
        $Destination = Join-Path $StagePath $EmptyDirectory
        New-Item -ItemType Directory -Path $Destination | Out-Null
        Copy-Item -LiteralPath (Join-Path $ProjectRoot "$EmptyDirectory\.gitkeep") -Destination $Destination
    }

    $StagePrefix = $StagePath.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $CacheDirectories = Get-ChildItem -LiteralPath $StagePath -Directory -Recurse -Force |
        Where-Object { $_.Name -eq "__pycache__" -or $_.Name -like "*.egg-info" } |
        Sort-Object FullName -Descending
    foreach ($CacheDirectory in $CacheDirectories) {
        if (-not $CacheDirectory.FullName.StartsWith($StagePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a cache path outside staging: $($CacheDirectory.FullName)"
        }
        Remove-Item -LiteralPath $CacheDirectory.FullName -Recurse -Force
    }
    Get-ChildItem -LiteralPath $StagePath -File -Recurse -Force |
        Where-Object { $_.Extension -eq ".pyc" } |
        Remove-Item -Force

    $FileCount = (Get-ChildItem -LiteralPath $StagePath -File -Recurse -Force).Count
    Compress-Archive -Path (Join-Path $StagePath "*") -DestinationPath $ArchivePath -CompressionLevel Optimal -Force

    New-Item -ItemType Directory -Path $VerifyPath | Out-Null
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $VerifyPath
    foreach ($RequiredPath in $RequiredArchivePaths) {
        if (-not (Test-Path -LiteralPath (Join-Path $VerifyPath $RequiredPath))) {
            throw "Archive verification failed; missing: $RequiredPath"
        }
    }

    Push-Location $VerifyPath
    $PreviousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = Join-Path $VerifyPath "src"
        & $ProjectPython -m compileall -q src tests
        if ($LASTEXITCODE -ne 0) {
            throw "Archive compile check failed"
        }
        & $ProjectPython -m finance_lab.cli --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Archive CLI smoke test failed"
        }
        & $ProjectPython -m pytest -q
        if ($LASTEXITCODE -ne 0) {
            throw "Archive test suite failed"
        }
    }
    finally {
        $env:PYTHONPATH = $PreviousPythonPath
        Pop-Location
    }

    $Hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash
    Write-Host "Source release archive verified: $ArchivePath" -ForegroundColor Green
    Write-Host "Files: $FileCount"
    Write-Host "SHA256: $Hash"
}
finally {
    Remove-SafeTemporaryDirectory -Path $StagePath -ExpectedLeaf $StageLeaf
    Remove-SafeTemporaryDirectory -Path $VerifyPath -ExpectedLeaf $VerifyLeaf
}
