# Register (or remove) a per-user Windows scheduled task that runs the bridge
# once a minute, so CC Switch's usage dashboard always reads fresh Qoder data.
#
# Uses schtasks so no administrator rights are needed.
#
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Interval 2
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall

param(
    [switch]$Uninstall,
    [int]$Interval = 1,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "QoderUsageBridge"
$Script = Join-Path $PSScriptRoot "qoder_usage_bridge.py"

if ($Uninstall) {
    schtasks /delete /tn $TaskName /f 2>$null | Out-Null
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

if (-not (Test-Path $Script)) {
    throw "qoder_usage_bridge.py not found next to this script."
}

if (-not $Python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    }
    if (-not $cmd) {
        throw "Python not found in PATH. Pass -Python <path to pythonw.exe>."
    }
    $Python = $cmd.Source
}

# pythonw.exe keeps the window hidden; --quiet sends output to the log file.
$command = "`"$Python`" `"$Script`" --apply --allow-running --quiet"

schtasks /create /tn $TaskName /tr $command /sc minute /mo $Interval /f | Out-Null

Write-Host "Installed scheduled task '$TaskName' (every $Interval minute(s))."
Write-Host "  python : $Python"
Write-Host "  script : $Script"
Write-Host "Logs   : $env:LOCALAPPDATA\qoder-usage-bridge\bridge.log"
Write-Host "Ledger : $env:LOCALAPPDATA\qoder-usage-bridge\qoder-ledger.db (appended every run)"
Write-Host "Snapshot: $env:LOCALAPPDATA\qoder-usage-bridge\snapshots\ (one dated copy per day)"
Write-Host "Run now: schtasks /run /tn $TaskName"
Write-Host "Remove : powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall"
