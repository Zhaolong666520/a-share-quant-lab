param(
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$AsOfDate = (Get-Date -Format "yyyy-MM-dd"),
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$StartDate = "2018-01-01",
    [ValidateRange(0, 30)]
    [int]$StaleAfterBusinessDays = 3,
    [ValidatePattern("^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    [string]$Portfolio = "default"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDirectory = Join-Path $ProjectRoot "outputs\paper_automation_logs"
$LogPath = Join-Path $LogDirectory "$Portfolio-$(Get-Date -Format 'yyyy-MM').log"

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
    Write-AutomationLog "[$(Get-Date -Format 'o')] status=started as_of=$AsOfDate portfolio=$Portfolio"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Python environment not found: $Python"
    }
    Set-Location $ProjectRoot
    $commandOutput = & $Python -m finance_lab.cli paper-daily `
        --portfolio $Portfolio `
        --start $StartDate `
        --as-of $AsOfDate `
        --stale-after-business-days $StaleAfterBusinessDays 2>&1
    $nativeExitCode = $LASTEXITCODE
    if ($null -ne $commandOutput) {
        $renderedOutput = ($commandOutput | Out-String).TrimEnd()
        Write-AutomationLog $renderedOutput
        Write-Output $renderedOutput
    }
    if ($nativeExitCode -ne 0) {
        throw "paper-daily failed with exit code $nativeExitCode"
    }
    Write-AutomationLog "[$(Get-Date -Format 'o')] status=succeeded"
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-AutomationLog "[$(Get-Date -Format 'o')] status=failed exit_code=$nativeExitCode message=$message"
    Show-PaperFailureNotification $message
    [Console]::Error.WriteLine("Paper daily automation failed: $message")
    exit $nativeExitCode
}
