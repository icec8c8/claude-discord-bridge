# Install Discord bridge as a Windows Scheduled Task that runs at user logon.
# Run this as the same user that owns ~\.claude-bridge\ — no admin needed.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#
# What it does:
#   - Creates a Scheduled Task "ClaudeDiscordBridge"
#   - Trigger: at user logon (current user)
#   - Action: pythonw.exe (windowless) running bridge.py from this folder
#   - Restart on failure: every 1 minute, up to 999 times
#   - Hidden, no time limit, runs even on battery
#
# To remove:
#   Unregister-ScheduledTask -TaskName "ClaudeDiscordBridge" -Confirm:$false

$ErrorActionPreference = "Stop"

$BridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonW   = Join-Path $BridgeDir "venv\Scripts\pythonw.exe"
$Script    = Join-Path $BridgeDir "bridge.py"
$TaskName  = "ClaudeDiscordBridge"

Write-Host "Bridge dir : $BridgeDir"
Write-Host "pythonw.exe: $PythonW"
Write-Host "bridge.py  : $Script"
Write-Host "task name  : $TaskName"
Write-Host ""

if (-not (Test-Path $PythonW)) { throw "pythonw.exe not found at $PythonW" }
if (-not (Test-Path $Script))  { throw "bridge.py not found at $Script" }

$action = New-ScheduledTaskAction `
    -Execute $PythonW `
    -Argument "`"$Script`"" `
    -WorkingDirectory $BridgeDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Hidden `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Register / replace
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Discord -> Claude Code auto-reply bridge (bridge.py)" `
    -Force | Out-Null

Write-Host "✓ Registered scheduled task '$TaskName'."
Write-Host ""
Write-Host "Status:"
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State, Author
Write-Host ""
Write-Host "To start it now without waiting for next logon:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "To stop / disable / remove:"
Write-Host "  Stop-ScheduledTask    -TaskName $TaskName"
Write-Host "  Disable-ScheduledTask -TaskName $TaskName"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
