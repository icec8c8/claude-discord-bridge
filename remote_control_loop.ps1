# Keep `claude remote-control` (server mode) running so the Claude mobile app
# can always reach this PC. Started hidden by the Scheduled Task that
# install_remote_control.ps1 registers — you normally don't run this by hand.
#
# What it does:
#   - Runs `claude remote-control` in -WorkDir with stdin = NUL, so a missing
#     first-run confirmation or workspace trust fails fast instead of
#     waiting forever on a prompt nobody can see
#   - Appends all output to remote-control.log next to this script
#     (rotated to remote-control.log.1 at 5 MB)
#   - Restarts it whenever it exits (server mode gives up after ~10 min
#     without network). Waits 10 s after a long run, doubling up to 10 min
#     while it keeps failing fast
#   - Records the cmd.exe PID in remote-control.pid and, on startup, kills a
#     tree left over from a previous run (Stop-ScheduledTask only kills this
#     PowerShell process, not its children)

param(
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [Parameter(Mandatory = $true)][string]$ClaudeExe,
    [string]$Name = "",
    [string]$ExtraArgs = ""
)

$ErrorActionPreference = "Stop"

$BridgeDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log         = Join-Path $BridgeDir "remote-control.log"
$PidFile     = Join-Path $BridgeDir "remote-control.pid"
$MaxLogBytes = 5MB
$Utf8NoBom   = New-Object System.Text.UTF8Encoding $false

function Write-Log([string]$Message) {
    $line = "{0} [loop] {1}`r`n" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    try { [System.IO.File]::AppendAllText($Log, $line, $Utf8NoBom) } catch { }
}

function Invoke-LogRotate {
    try {
        if ((Test-Path $Log) -and (Get-Item $Log).Length -gt $MaxLogBytes) {
            Move-Item -Force $Log "$Log.1"
        }
    } catch { }
}

function Stop-Tree([int]$ProcId) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcId" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Tree $_.ProcessId }
    Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
}

function Stop-Leftover {
    if (-not (Test-Path $PidFile)) { return }
    $old = (Get-Content $PidFile -Raw).Trim()
    Remove-Item -Force $PidFile
    if ($old -notmatch '^\d+$') { return }
    # Only kill it if the PID still belongs to our cmd.exe (PIDs get reused)
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$old" -ErrorAction SilentlyContinue
    if ($proc -and $proc.Name -eq "cmd.exe" -and $proc.CommandLine -like "*remote-control*") {
        Write-Log "killing leftover remote-control tree (cmd.exe pid $old)"
        Stop-Tree ([int]$old)
    }
}

# Remote Control only works with a claude.ai login. With any of these set,
# claude authenticates with the key/token instead and the server refuses to
# start, so drop them for this process only.
foreach ($var in "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN") {
    if (Test-Path "Env:$var") {
        Write-Log "unsetting $var for this process (Remote Control needs a claude.ai login)"
        Remove-Item "Env:$var"
    }
}

$rcArgs = "remote-control"
if ($Name)      { $rcArgs += " --name `"$Name`"" }
if ($ExtraArgs) { $rcArgs += " $ExtraArgs" }

Stop-Leftover

$delay = 10
while ($true) {
    Invoke-LogRotate
    Write-Log "starting: `"$ClaudeExe`" $rcArgs (cwd $WorkDir)"
    $started = Get-Date

    $code = "?"
    try {
        # cmd /s strips the outer quotes. Going through cmd gives us < NUL and a
        # byte-for-byte >> append, which Windows PowerShell 5.1 can't do natively.
        $inner = "`"$ClaudeExe`" $rcArgs < NUL >> `"$Log`" 2>&1"
        $p = Start-Process -FilePath "cmd.exe" -ArgumentList "/d /s /c `"$inner`"" `
            -WorkingDirectory $WorkDir -NoNewWindow -PassThru
        $null = $p.Handle  # without this, ExitCode is empty once the process exits
        Set-Content -Path $PidFile -Value $p.Id -Encoding ASCII
        $p.WaitForExit()
        $code = $p.ExitCode
    } catch {
        Write-Log "could not start claude: $_"
    }
    Remove-Item -Force $PidFile -ErrorAction SilentlyContinue

    $ran = [int]((Get-Date) - $started).TotalSeconds
    if ($ran -ge 300) { $delay = 10 } else { $delay = [Math]::Min($delay * 2, 600) }
    Write-Log "exited with code $code after ${ran}s; restarting in ${delay}s"
    Start-Sleep -Seconds $delay
}
