# Manage Windows Scheduled Tasks for Tango Coach check-ins.
#
# Each task fires checkin.ps1, which opens Claude already running a tango
# check-in. Three kinds: predance (before class/practica/milonga),
# reflect (post-dancing reflection), weekly (Sunday review).
#
# Usage:
#   .\schedule.ps1 install -Time "18:00" -Day "Tuesday" -Kind predance -Name "Tue practica"
#   .\schedule.ps1 install -Time "10:00" -Day "Saturday" -Kind reflect  -Name "Fri milonga recap"
#   .\schedule.ps1 install -Time "19:00" -Day "Sunday"   -Kind weekly   -Name "weekly review"
#   .\schedule.ps1 install -Time "18:00"                                  # daily, default kind=predance
#   .\schedule.ps1 list
#   .\schedule.ps1 uninstall                                              # removes ALL Tango Coach tasks
#   .\schedule.ps1 uninstall -Name "weekly review"                        # removes one by name
#   .\schedule.ps1 run -Kind reflect                                      # fire once now (test)
#
# Day names: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday.

param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("install", "list", "uninstall", "run")]
    [string]$Action,

    [string]$Time = "18:00",
    [string]$Name = $null,
    [ValidateSet("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday")]
    [string]$Day = $null,
    [ValidateSet("predance","reflect","weekly")]
    [string]$Kind = "predance"
)

# Default name: day-of-week if specified, else the kind.
if (-not $Name) {
    if ($Day) { $Name = $Day } else { $Name = $Kind }
}

$ErrorActionPreference = "Stop"

$checkinScript = Join-Path $PSScriptRoot "checkin.ps1"
$taskPrefix = "Tango Coach"

function Get-TaskName { param([string]$N) "$taskPrefix - $N check-in" }

switch ($Action) {

    "install" {
        if (-not (Test-Path $checkinScript)) {
            Write-Error "checkin.ps1 not found at $checkinScript"
            exit 1
        }

        $taskName = Get-TaskName $Name

        $taskAction = New-ScheduledTaskAction `
            -Execute "powershell.exe" `
            -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$checkinScript`" -Kind $Kind"

        if ($Day) {
            $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $Time
        } else {
            $trigger = New-ScheduledTaskTrigger -Daily -At $Time
        }

        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1)

        $principal = New-ScheduledTaskPrincipal `
            -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType Interactive `
            -RunLevel Limited

        # Replace if already registered with this name
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $taskAction `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal | Out-Null

        if ($Day) {
            Write-Host "Installed: '$taskName' (kind=$Kind) -> runs every $Day at $Time when you are logged on."
        } else {
            Write-Host "Installed: '$taskName' (kind=$Kind) -> runs daily at $Time when you are logged on."
        }
        Write-Host "Edit it later in Windows Task Scheduler (taskschd.msc) or via this script."
    }

    "list" {
        $tasks = Get-ScheduledTask | Where-Object { $_.TaskName -like "$taskPrefix*" }
        if (-not $tasks) {
            Write-Host "No Tango Coach tasks installed."
            return
        }
        foreach ($t in $tasks) {
            $info = Get-ScheduledTaskInfo -TaskName $t.TaskName
            Write-Host ("{0,-44} state={1,-7} last_run={2}" -f $t.TaskName, $t.State, $info.LastRunTime)
        }
    }

    "uninstall" {
        if ($PSBoundParameters.ContainsKey('Name')) {
            $target = Get-TaskName $Name
            Unregister-ScheduledTask -TaskName $target -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "Removed: $target"
        } else {
            $tasks = Get-ScheduledTask | Where-Object { $_.TaskName -like "$taskPrefix*" }
            foreach ($t in $tasks) {
                Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
                Write-Host "Removed: $($t.TaskName)"
            }
            if (-not $tasks) { Write-Host "Nothing to remove." }
        }
    }

    "run" {
        Write-Host "Firing checkin.ps1 once (kind=$Kind) for testing..."
        & $checkinScript -Kind $Kind
    }
}
