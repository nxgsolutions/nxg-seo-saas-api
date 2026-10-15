# Register the background data jobs in Windows Task Scheduler (current user).
#
#   SEO gsc_sync   daily  02:00  Search Console -> rank history, keywords, metrics (free)
#
# The paid weekly SERP sync (run_serp_sync.bat) is NOT registered: it is switched off
# (SERP_SYNC_ENABLED=false). To add it later, uncomment its line below and set
# SERP_SYNC_ENABLED=true in backend/.env.
#
# Run from PowerShell:   powershell -ExecutionPolicy Bypass -File D:\seo\backend\scripts\register_windows_jobs.ps1
# Remove again:     powershell -ExecutionPolicy Bypass -File D:\seo\backend\scripts\register_windows_jobs.ps1 -Remove
#
# Tasks run only while you are logged in (no stored password). "StartWhenAvailable" runs a
# missed job as soon as the PC is on again. Each run is logged in the job_runs table and in
# backend\logs\<job>.log.

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$scripts = $PSScriptRoot
$backend = Split-Path $scripts -Parent
$logs = Join-Path $backend "logs"

$jobs = @(
    @{ Name = "SEO gsc_sync";  Bat = "run_gsc_sync.bat";  Trigger = (New-ScheduledTaskTrigger -Daily -At "02:00") }
    # Paid - off for now:
    # @{ Name = "SEO serp_sync"; Bat = "run_serp_sync.bat"; Trigger = (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "03:00") }
)

if ($Remove) {
    foreach ($j in $jobs) {
        if (Get-ScheduledTask -TaskName $j.Name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $j.Name -Confirm:$false
            Write-Host "Removed: $($j.Name)"
        }
    }
    return
}

New-Item -ItemType Directory -Force $logs | Out-Null
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

foreach ($j in $jobs) {
    $bat = Join-Path $scripts $j.Bat
    $log = Join-Path $logs (($j.Bat -replace '^run_', '') -replace '\.bat$', '.log')
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$bat`" >> `"$log`" 2>&1`"" -WorkingDirectory $backend
    Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $j.Trigger -Settings $settings `
        -Description "NXGSEO background job ($($j.Bat)). Logged in job_runs." -Force | Out-Null
    $next = (Get-ScheduledTaskInfo -TaskName $j.Name).NextRunTime
    Write-Host "Registered: $($j.Name)  (next run: $next, log: $log)"
}
Write-Host "`nRun one now to test:  Start-ScheduledTask -TaskName 'SEO gsc_sync'"
