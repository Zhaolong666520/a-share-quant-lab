param(
    [string]$TaskName = "A-Share Quant Lab Paper Daily",
    [ValidatePattern("^(?:[01]\d|2[0-3]):[0-5]\d$")]
    [string]$RunAt = "18:30",
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$StartDate = "2018-01-01",
    [ValidateRange(0, 30)]
    [int]$StaleAfterBusinessDays = 3,
    [ValidatePattern("^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    [string]$Portfolio = "default",
    [switch]$Uninstall,
    [string]$DataRoot
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Get-ValidatedDataRoot {
    param([string]$Root)
    if ([string]::IsNullOrWhiteSpace($Root) -or
        $Root -match '[\x00-\x1f"''`<>|?*]' -or
        $Root -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "Invalid DataRoot: use an existing ordinary filesystem directory."
    }
    $provider = $null
    $drive = $null
    $absolute = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        $Root, [ref]$provider, [ref]$drive
    )
    if ($provider.Name -ne "FileSystem") {
        throw "DataRoot must use the filesystem provider."
    }
    $item = Get-Item -LiteralPath $absolute -Force -ErrorAction Stop
    if ($item -isnot [System.IO.DirectoryInfo]) {
        throw "DataRoot must be an existing directory."
    }
    while ($null -ne $item) {
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "DataRoot and its ancestors must not be links or reparse points."
        }
        $item = $item.Parent
    }
    return $absolute
}

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Scheduled task removed: $TaskName" -ForegroundColor Green
    } else {
        Write-Host "Scheduled task is not installed: $TaskName" -ForegroundColor Yellow
    }
    exit 0
}

if ($ProjectRoot -match "[\\/]\.worktrees[\\/]") {
    throw "Refusing to register from .worktrees. Merge into a stable project path first."
}

$Runner = Join-Path $PSScriptRoot "run_paper_daily.ps1"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Daily runner not found: $Runner"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python environment not found. Run setup.cmd first: $Python"
}

$argument = (
    "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass " +
    "-File `"$Runner`" -StartDate $StartDate " +
    "-StaleAfterBusinessDays $StaleAfterBusinessDays -Portfolio $Portfolio"
)
if ($PSBoundParameters.ContainsKey("DataRoot")) {
    $ResolvedDataRoot = Get-ValidatedDataRoot $DataRoot
    # Native parsing requires doubled trailing backslashes before a closing quote.
    $NativeDataRoot = $ResolvedDataRoot -replace '(\\+)$', '$1$1'
    $argument += " -DataRoot `"$NativeDataRoot`""
}
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $argument `
    -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At $RunAt
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Research only: update data and run local paper accounts; no broker connection." `
    -Force | Out-Null

Write-Host "Scheduled task installed: $TaskName" -ForegroundColor Green
Write-Host "Schedule: weekdays at $RunAt in the local time zone"
Write-Host "Failures show a local notification; details are stored under outputs."
Write-Host "Uninstall: .\scripts\install_paper_daily_task.ps1 -Uninstall"
