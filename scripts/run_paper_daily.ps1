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
$LaunchDirectory = $null
$LaunchId = [Guid]::NewGuid().ToString('N')
$ChildIdentity = $null
$ChildExitCode = $null
$FailureStage = 'preflight'

function Assert-NoLinkPath {
    param([string]$Path)
    $cursor = [System.IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw 'Process evidence path contains a reparse point.'
            }
        }
        $cursor = Split-Path -Parent $cursor
    }
}

function Write-ProcessRecord {
    param([string]$Name, [string]$Kind, [hashtable]$Details)
    Assert-NoLinkPath $LaunchDirectory
    $record = @{
        schema_version=1;kind=$Kind;launch_id=$LaunchId;portfolio_id=$Portfolio;
        as_of_date=$AsOfDate;root=$ResolvedDataRoot;recorded_at=[DateTimeOffset]::UtcNow.ToString('o')
    }
    foreach ($key in $Details.Keys) { $record[$key] = $Details[$key] }
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes(($record | ConvertTo-Json -Depth 8 -Compress))
    $path = Join-Path $LaunchDirectory $Name
    Assert-NoLinkPath $path
    $temporary = "$path.tmp"
    Assert-NoLinkPath $temporary
    $stream = [System.IO.File]::Open($temporary, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    [System.IO.File]::Move($temporary, $path)
}

function Get-ScriptDigest {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $inputStream = [System.IO.File]::OpenRead($PSCommandPath)
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($inputStream))).Replace('-', '').ToLowerInvariant()
    } finally { $inputStream.Dispose(); $algorithm.Dispose() }
}

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
    Assert-NoLinkPath $LogDirectory
    Assert-NoLinkPath $LogPath
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
    $LaunchDirectory = Join-Path $ResolvedDataRoot "outputs\paper_process_runs\$Portfolio\$LaunchId"
    Assert-NoLinkPath $LaunchDirectory
    if (Test-Path -LiteralPath $LaunchDirectory) { throw 'Launch directory already exists.' }
    New-Item -ItemType Directory -Path $LaunchDirectory | Out-Null
    $wrapper = Get-Process -Id $PID
    Write-ProcessRecord 'launcher.json' 'launcher_started' @{
        wrapper=@{pid=$PID;creation_filetime=$wrapper.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()};
        expected_python=$Python;script_path=$PSCommandPath;script_sha256=(Get-ScriptDigest)
    }
    Write-AutomationLog "[$(Get-Date -Format 'o')] status=started as_of=$AsOfDate portfolio=$Portfolio"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Python environment not found: $Python"
    }
    Set-Location $ProjectRoot
    $FailureStage = 'launch_child'
    $arguments = @('-B', '-u', '-X', 'faulthandler', '-m', 'finance_lab.paper_process_worker', 'paper-daily',
        '--portfolio', $Portfolio, '--start', $StartDate, '--as-of', $AsOfDate,
        '--stale-after-business-days', $StaleAfterBusinessDays.ToString(), '--launch-id', $LaunchId)
    $child = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden -PassThru
    # Keep a handle before waiting so a fast exit still has a readable exit code.
    $childHandle = $child.Handle
    $ChildIdentity = @{pid=$child.Id;creation_filetime=$child.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()}
    $FailureStage = 'record_child'
    Write-ProcessRecord 'child.json' 'child_started' @{child=$ChildIdentity}
    $FailureStage = 'wait_child'
    $child.WaitForExit()
    $child.Refresh()
    if ($null -eq $child.ExitCode) { throw 'Child exit code is unavailable.' }
    $ChildExitCode = [int]$child.ExitCode
    $nativeExitCode = $ChildExitCode
    $FailureStage = 'child_exit'
    $unsigned = [BitConverter]::ToUInt32([BitConverter]::GetBytes($ChildExitCode), 0)
    Write-ProcessRecord 'child-exit.json' 'child_exited' @{
        child=$ChildIdentity;exit_code=$ChildExitCode;exit_code_uint32=[long]$unsigned;
        exit_code_hex=('0x{0:X8}' -f $unsigned)
    }
    if ($nativeExitCode -ne 0) {
        throw "paper-daily failed with exit code $nativeExitCode"
    }
    Write-AutomationLog "[$(Get-Date -Format 'o')] status=succeeded"
    $nativeExitCode = 0
} catch {
    $message = $_.Exception.Message
    if ($nativeExitCode -eq 0) { $nativeExitCode = 1 }
    if ($null -ne $LogPath) {
        try {
            Write-AutomationLog "[$(Get-Date -Format 'o')] status=failed exit_code=$nativeExitCode message=$message"
        } catch { [Console]::Error.WriteLine('Failure log could not be written.') }
        try { Show-PaperFailureNotification $message } catch {
            [Console]::Error.WriteLine('Failure notification could not be shown.')
        }
    }
    [Console]::Error.WriteLine("Paper daily automation failed: $message")
} finally {
    if ($null -ne $LaunchDirectory -and (Test-Path -LiteralPath $LaunchDirectory)) {
        try {
            Write-ProcessRecord 'wrapper-exit.json' 'wrapper_exited' @{
                exit_code=$nativeExitCode;child_exit_code=$ChildExitCode;stage=$FailureStage;
                status=$(if ($nativeExitCode -eq 0) {'succeeded'} else {'failed'})
            }
        } catch {
            [Console]::Error.WriteLine('Wrapper exit receipt could not be written.')
            if ($nativeExitCode -eq 0) { $nativeExitCode = 1 }
        }
    }
    $env:FINANCE_LAB_ROOT = $PreviousDataRoot
    $env:PYTHONUTF8 = $PreviousPythonUtf8
    Set-Location -LiteralPath $PreviousLocation.Path
}
exit $nativeExitCode
