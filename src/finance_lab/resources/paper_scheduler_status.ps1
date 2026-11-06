param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9 _.-]{0,127}$')]
    [string]$TaskName = 'A-Share Quant Lab Paper Daily'
)

# Query only. This script neither starts tasks nor changes their configuration.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function ConvertTo-UtcStamp {
    param($Value)
    if ($null -eq $Value -or $Value.Year -lt 1971) { return $null }
    return $Value.ToUniversalTime().ToString('o')
}

function Get-ObservationSignature {
    param($Task, $Info)
    # Keep action arguments private: compare them locally, never publish them.
    [ordered]@{
        state = [string]$Task.State
        actions = @($Task.Actions | Select-Object Execute, Arguments, WorkingDirectory)
        enabled = $Task.Settings.Enabled
        start_when_available = $Task.Settings.StartWhenAvailable
        disallow_start_on_batteries = $Task.Settings.DisallowStartIfOnBatteries
        stop_on_batteries = $Task.Settings.StopIfGoingOnBatteries
        wake_to_run = $Task.Settings.WakeToRun
        last_run_time = ConvertTo-UtcStamp $Info.LastRunTime
        last_task_result = $Info.LastTaskResult
        next_run_time = ConvertTo-UtcStamp $Info.NextRunTime
        missed_runs = $Info.NumberOfMissedRuns
    } | ConvertTo-Json -Depth 4 -Compress
}

try {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop
    $info = Get-ScheduledTaskInfo -InputObject $task -ErrorAction Stop
    $before = Get-ObservationSignature $task $info
    $origin = 'unknown'
    if ($null -ne $info.LastRunTime -and $info.LastRunTime.Year -ge 1971) {
        try {
            $events = @(Get-WinEvent -FilterHashtable @{
                LogName = 'Microsoft-Windows-TaskScheduler/Operational'
                StartTime = $info.LastRunTime.AddSeconds(-2)
                EndTime = $info.LastRunTime.AddSeconds(2)
                Id = 107, 110
            } -MaxEvents 100 -ErrorAction Stop)
            $matching = @($events | ForEach-Object {
                [xml]$eventXml = $_.ToXml()
                $name = @($eventXml.Event.EventData.Data | Where-Object { $_.Name -eq 'TaskName' })
                if ($name.Count -eq 1 -and $name[0].'#text' -ceq ('\' + $TaskName)) { $_ }
            })
            # No selection between ambiguous or incomplete observations.
            if ($events.Count -lt 100 -and $matching.Count -eq 1) {
                if ($matching[0].Id -eq 107) { $origin = 'time_trigger' }
                elseif ($matching[0].Id -eq 110) { $origin = 'manual' }
            }
        } catch {
            $origin = 'unknown'
        }
    }
    $afterTask = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop
    $afterInfo = Get-ScheduledTaskInfo -InputObject $afterTask -ErrorAction Stop
    $after = Get-ObservationSignature $afterTask $afterInfo
    # Two observations detect visible changes, not ABA or an atomic snapshot.
    if ($before -cne $after) {
        $result = [ordered]@{ schema_version = 1; status = 'unknown'; reason = 'query_changed' }
    } else {
      $result = [ordered]@{
        schema_version = 1
        status = 'available'
        task_name = $TaskName
        observed_at = [DateTime]::UtcNow.ToString('o')
        state = [string]$task.State
        enabled = $task.Settings.Enabled
        last_run_time = ConvertTo-UtcStamp $info.LastRunTime
        last_task_result = $info.LastTaskResult
        next_run_time = ConvertTo-UtcStamp $info.NextRunTime
        missed_runs = $info.NumberOfMissedRuns
        start_when_available = $task.Settings.StartWhenAvailable
        disallow_start_on_batteries = $task.Settings.DisallowStartIfOnBatteries
        stop_on_batteries = $task.Settings.StopIfGoingOnBatteries
        wake_to_run = $task.Settings.WakeToRun
        trigger_origin = $origin
      }
    }
} catch {
    $result = [ordered]@{ schema_version = 1; status = 'unknown'; reason = 'query_unavailable' }
}
$result | ConvertTo-Json -Depth 3 -Compress
