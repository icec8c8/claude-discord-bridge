# Keep `claude remote-control` running on this PC so the Claude mobile app
# (Code tab) or claude.ai/code can start Claude Code sessions here.
# Registers a Scheduled Task that runs remote_control_loop.ps1 hidden at
# user logon. Run it as yourself — no admin needed.
#
# ONE-TIME SETUP FIRST (needs a real terminal, cannot be done unattended):
#   mkdir $HOME\claude-remote
#   cd $HOME\claude-remote
#   claude remote-control
#   -> answer y to "Enable Remote Control?" and to "Trust <folder>?",
#      wait until the session URL shows, then press Ctrl+C
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_remote_control.ps1
#   powershell -ExecutionPolicy Bypass -File install_remote_control.ps1 -WorkDir D:\projects
#   powershell -ExecutionPolicy Bypass -File install_remote_control.ps1 -Name "Home PC" -ExtraArgs "--permission-mode acceptEdits"
#
# Options:
#   -WorkDir    folder the sessions start in (default: $HOME\claude-remote).
#               Not your home folder itself: Claude Code never saves trust
#               for it, so the server could not start unattended there.
#   -Name       session title shown in the app (default: <hostname>-<random>)
#   -ExtraArgs  extra `claude remote-control` flags, plain words only
#   -ClaudeExe  path to claude.exe / claude.cmd (default: found on PATH)
#
# To remove (also stops the running server):
#   powershell -ExecutionPolicy Bypass -File install_remote_control.ps1 -Uninstall

param(
    [string]$WorkDir = (Join-Path $HOME "claude-remote"),
    [string]$Name = "",
    [string]$ExtraArgs = "",
    [string]$ClaudeExe = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$BridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LoopPs1   = Join-Path $BridgeDir "remote_control_loop.ps1"
$Log       = Join-Path $BridgeDir "remote-control.log"
$PidFile   = Join-Path $BridgeDir "remote-control.pid"
$TaskName  = "ClaudeRemoteControl"

function Stop-Tree([int]$ProcId) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcId" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Tree $_.ProcessId }
    Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "OK: removed scheduled task '$TaskName'."
    } else {
        Write-Host "Scheduled task '$TaskName' is not installed."
    }
    # Stopping the task only kills the PowerShell loop; claude keeps running
    if (Test-Path $PidFile) {
        $old = (Get-Content $PidFile -Raw).Trim()
        Remove-Item -Force $PidFile
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$old" -ErrorAction SilentlyContinue
        if ($proc -and $proc.Name -eq "cmd.exe" -and $proc.CommandLine -like "*remote-control*") {
            Stop-Tree ([int]$old)
            Write-Host "OK: stopped claude remote-control (cmd.exe pid $old)."
        }
    }
    exit 0
}

if (-not (Test-Path $LoopPs1)) { throw "remote_control_loop.ps1 not found at $LoopPs1" }

if (-not $ClaudeExe) {
    # Prefer the native claude.exe, then npm's claude.cmd; skip npm's claude.ps1
    $found = Get-Command claude.exe, claude.cmd -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $found) { throw "claude not found on PATH. Install Claude Code first, or pass -ClaudeExe <path>." }
    $ClaudeExe = $found.Source
}
if (-not (Test-Path $ClaudeExe)) { throw "claude not found at $ClaudeExe" }

if (-not (Test-Path $WorkDir -PathType Container)) {
    throw "WorkDir $WorkDir does not exist. Do the one-time setup at the top of this script first."
}
$WorkDir = (Resolve-Path $WorkDir).Path
if ($WorkDir.TrimEnd('\') -eq $HOME.TrimEnd('\')) {
    throw "WorkDir cannot be your home folder: Claude Code never saves trust for it, so the server could not start unattended. Use a subfolder such as $HOME\claude-remote."
}

# The "Enable Remote Control?" answer is stored as remoteDialogSeen in
# ~/.claude.json. Without it the server can't start unattended.
$ConfigDir    = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { $HOME }
$GlobalConfig = Join-Path $ConfigDir ".claude.json"
$consented = (Test-Path $GlobalConfig) -and
    ((Get-Content $GlobalConfig -Raw) -match '"remoteDialogSeen"\s*:\s*true')
if (-not $consented) {
    Write-Warning "Remote Control has not been enabled on this PC yet. Run this once first:"
    Write-Host "  cd `"$WorkDir`""
    Write-Host "  claude remote-control"
    Write-Host "  (answer y to both questions, wait for the URL, then press Ctrl+C)"
    $yn = Read-Host "Continue anyway? [y/N]"
    if ($yn -notmatch '^[yY]') { exit 1 }
}

Write-Host "Bridge dir : $BridgeDir"
Write-Host "claude     : $ClaudeExe"
Write-Host "work dir   : $WorkDir"
Write-Host "task name  : $TaskName"
Write-Host ""

# A trailing \ before the closing quote (e.g. D:\) would escape it
$wdArg = if ($WorkDir.EndsWith('\')) { "$WorkDir." } else { $WorkDir }
$loopArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden " +
            "-File `"$LoopPs1`" -WorkDir `"$wdArg`" -ClaudeExe `"$ClaudeExe`""
# Colon form so a value starting with "--" isn't read as a parameter name
if ($Name)      { $loopArgs += " -Name:`"$Name`"" }
if ($ExtraArgs) { $loopArgs += " -ExtraArgs:`"$ExtraArgs`"" }

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $loopArgs `
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

# Replace a previous install cleanly so two servers never share one folder
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Claude Code Remote Control server (remote_control_loop.ps1)" `
    -Force | Out-Null

Write-Host "OK: registered scheduled task '$TaskName'. Starting it now..."
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 20
Write-Host ""
Write-Host "=== Last lines of remote-control.log ==="
if (Test-Path $Log) { Get-Content $Log -Tail 20 -Encoding UTF8 } else { Write-Host "(no log yet)" }
Write-Host ""
Write-Host "If you see 'Workspace not trusted', 'Enable Remote Control?' or a login error,"
Write-Host "fix it in a normal terminal (see the top of this script) and re-run the installer."
Write-Host "Otherwise open the Claude app -> Code: this PC shows up with a green dot."
Write-Host ""
Write-Host "Log      : $Log"
Write-Host "Restart  : Stop-ScheduledTask -TaskName $TaskName; Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove   : powershell -ExecutionPolicy Bypass -File install_remote_control.ps1 -Uninstall"
