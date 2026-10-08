param(
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$AsOfDate = (Get-Date -Format "yyyy-MM-dd"),
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$StartDate = "2018-01-01",
    [ValidateRange(0, 30)]
    [int]$StaleAfterBusinessDays = 3,
    [ValidatePattern("^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    [string]$Portfolio = "default",
    [string]$DataRoot
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PreviousDataRoot = $env:FINANCE_LAB_ROOT
$PreviousPythonUtf8 = $env:PYTHONUTF8
$PreviousLocation = Get-Location
$LogPath = $null

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

function Write-AutomationLog {
    param([string]$Message)
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8
}

function Show-PaperFailureNotification {
    param([string]$Message)
    $notification = $null
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $notification = [System.Windows.Forms.NotifyIcon]::new()
        $notification.Icon = [System.Drawing.SystemIcons]::Error
        $notification.Visible = $true
        $notification.BalloonTipTitle = "A-Share Quant Lab daily task failed"
        $notification.BalloonTipText = $Message
        $notification.ShowBalloonTip(10000)
        Start-Sleep -Seconds 5
    } catch {
        Write-AutomationLog "[$(Get-Date -Format 'o')] notification_warning=$($_.Exception.Message)"
    } finally {
        if ($null -ne $notification) {
            $notification.Dispose()
        }
    }
}

$nativeExitCode = 1
try {
    $selectedRoot = $ProjectRoot
    if ($PSBoundParameters.ContainsKey("DataRoot")) {
        $selectedRoot = $DataRoot
    } elseif ($null -ne $PreviousDataRoot) {
        $selectedRoot = $PreviousDataRoot
    }
    $ResolvedDataRoot = Get-ValidatedDataRoot $selectedRoot
    $LogDirectory = Join-Path $ResolvedDataRoot "outputs\paper_automation_logs"
    $LogPath = Join-Path $LogDirectory "$Portfolio-$(Get-Date -Format 'yyyy-MM').log"
    $env:FINANCE_LAB_ROOT = $ResolvedDataRoot
    $env:PYTHONUTF8 = "1"
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    Write-AutomationLog "[$(Get-Date -Format 'o')] status=started as_of=$AsOfDate portfolio=$Portfolio"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Python environment not found: $Python"
    }
    Set-Location $ProjectRoot
    $previousNativePreference = $ErrorActionPreference
    try {
        # Native stderr must not bypass capture of the actual Python exit code.
        $ErrorActionPreference = "Continue"
        $commandOutput = & $Python -B -m finance_lab.cli paper-daily `
            --portfolio $Portfolio `
            --start $StartDate `
            --as-of $AsOfDate `
            --stale-after-business-days $StaleAfterBusinessDays 2>&1
        $nativeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousNativePreference
    }
    if ($null -ne $commandOutput) {
        $renderedOutput = ($commandOutput | Out-String).TrimEnd()
        Write-AutomationLog $renderedOutput
        Write-Output $renderedOutput
    }
    if ($nativeExitCode -ne 0) {
        throw "paper-daily failed with exit code $nativeExitCode"
    }
    Write-AutomationLog "[$(Get-Date -Format 'o')] status=succeeded"
    $nativeExitCode = 0
} catch {
    $message = $_.Exception.Message
    if ($null -ne $LogPath) {
        Write-AutomationLog "[$(Get-Date -Format 'o')] status=failed exit_code=$nativeExitCode message=$message"
        Show-PaperFailureNotification $message
    }
    [Console]::Error.WriteLine("Paper daily automation failed: $message")
} finally {
    $env:FINANCE_LAB_ROOT = $PreviousDataRoot
    $env:PYTHONUTF8 = $PreviousPythonUtf8
    Set-Location -LiteralPath $PreviousLocation.Path
}
exit $nativeExitCode
