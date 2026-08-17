# Registers (or re-registers) the hourly jobradar scan in Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1
#
# Runs hourly while you are logged in, only on AC or battery, and catches up
# after the machine wakes. Remove with:  scripts\install_scheduler.ps1 -Uninstall

param(
    [switch]$Uninstall,
    [int]$IntervalMinutes = 60,
    # Run in an interactive session with a visible console. Useful for watching
    # a scan happen; not what you want day to day.
    [switch]$Visible
)

$taskName = "jobradar-hourly-scan"
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "scripts\run_scan.ps1"

function Remove-JobradarTask {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $existing) { return $false }
    if ($existing.State -eq "Running") {
        Write-Host "Stopping the in-progress scan..."
        try { Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop } catch {}
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    return $true
}

if ($Uninstall) {
    if (Remove-JobradarTask) {
        Write-Host "Removed scheduled task '$taskName'."
    } else {
        Write-Host "No scheduled task named '$taskName' found."
    }
    exit 0
}

if (-not (Test-Path $runner)) { throw "Cannot find $runner" }

# Launch pythonw.exe directly rather than going through PowerShell.
#
# pythonw.exe is a GUI-subsystem binary: Windows never allocates a console for
# it, so no window can appear. Routing through powershell.exe does show one -
# -WindowStyle Hidden is applied only after the console host has already been
# created, so the window flashes up regardless of the flag or the task's logon
# type. The connectivity check lives inside the scanner itself, so the
# PowerShell wrapper was never load-bearing; it stays for manual debug runs.
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if ($Visible -or -not (Test-Path $pythonw)) {
    if (-not (Test-Path $pythonw)) {
        Write-Host "pythonw.exe not found in .venv - falling back to the PowerShell wrapper." -ForegroundColor Yellow
    }
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`"" `
        -WorkingDirectory $root
    $launcher = "powershell wrapper (a console window will appear)"
} else {
    # --sync also merges whatever the GitHub Actions runner found while this
    # machine was asleep, so the two halves converge without you doing anything.
    # A failed sync is logged and ignored; the local scan still counts.
    $action = New-ScheduledTaskAction `
        -Execute $pythonw `
        -Argument "-m jobradar scan --sync" `
        -WorkingDirectory $root
    $launcher = "pythonw.exe (no console window), scan + cloud sync"
}

# Repeat indefinitely, starting a minute from now.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

Remove-JobradarTask | Out-Null

$description = "Scans company ATS boards and job boards for roles matching your jobradar profile."

# Interactive logon, which registers reliably on any machine. Hiding the window
# is the launcher's job, not the logon type's: an S4U task would also run
# without a window, but S4U needs the "Log on as a batch job" right and silently
# refuses to register on machines that withhold it.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description $description | Out-Null

Write-Host ""
Write-Host "Installed '$taskName' - every $IntervalMinutes minutes." -ForegroundColor Green
Write-Host "Launcher: $launcher" -ForegroundColor Green
Write-Host ""
Write-Host "  Run it now:      Start-ScheduledTask -TaskName $taskName"
Write-Host "  Check status:    Get-ScheduledTaskInfo -TaskName $taskName"
Write-Host "  Watch the log:   Get-Content data\scan.log -Wait -Tail 20"
Write-Host "  Remove:          scripts\install_scheduler.ps1 -Uninstall"
Write-Host ""
