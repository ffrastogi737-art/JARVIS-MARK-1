<#
.SYNOPSIS
    Installs the JARVIS Edge Agent as a 24/7 background task that starts on Windows boot (Phase 7).

.DESCRIPTION
    Registers a Task Scheduler task "JARVIS Edge Agent" that runs
    `pythonw -m brain.daemon.service_wrapper` at startup, as the current user, whether or not
    anyone is logged on, restarting it if it ever exits. Run from an elevated PowerShell.

.EXAMPLE
    .\install_daemon.ps1 -CloudUrl https://jarvis.example.com -Token <TETHER_SECRET_TOKEN>
    .\install_daemon.ps1 -Uninstall
#>
param(
    [string]$CloudUrl,
    [string]$Token,
    [string]$Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TaskName = "JARVIS Edge Agent"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this script from an elevated (Administrator) PowerShell." }

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable("JARVIS_CLOUD_URL", $null, "Machine")
    [Environment]::SetEnvironmentVariable("TETHER_SECRET_TOKEN", $null, "Machine")
    Write-Host "Removed '$TaskName'."
    return
}

if (-not $CloudUrl -or -not $Token) { throw "-CloudUrl and -Token are required." }
if (-not $Python) { throw "pythonw.exe not found on PATH; pass -Python <path to pythonw.exe>." }

# Machine scope so the boot-time task sees them. Note: readable by every local account.
[Environment]::SetEnvironmentVariable("JARVIS_CLOUD_URL", $CloudUrl, "Machine")
[Environment]::SetEnvironmentVariable("TETHER_SECRET_TOKEN", $Token, "Machine")

$action = New-ScheduledTaskAction -Execute $Python -Argument "-m brain.daemon.service_wrapper" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "JARVIS Phase 7 ultra-light Edge Agent" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started '$TaskName' (log: ~\.jarvis\edge.log). Local HALT: create ~\.jarvis\HALT"
