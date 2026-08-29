# Register a Windows Scheduled Task that runs watch_jobs.py once a day.
# 注册一个 Windows 计划任务，每天跑一次 watch_jobs.py。
#
# Usage / 用法:
#   .\setup_task.ps1                          # defaults: 09:07, auto-detect Python
#   .\setup_task.ps1 -RunAt "07:23"
#   .\setup_task.ps1 -Python "D:\miniforge3\envs\dc1\pythonw.exe"
#   .\setup_task.ps1 -TaskName "MyWatcher"
#
# Uninstall / 卸载:
#   Unregister-ScheduledTask -TaskName PredocWatcher -Confirm:$false

[CmdletBinding()]
param(
    [string]$TaskName = "PredocWatcher",
    [string]$RunAt    = "09:07",
    [string]$Python   = ""
)

$ErrorActionPreference = "Stop"

# Everything is resolved relative to this script, so the repo works wherever it is cloned.
# 所有路径都相对本脚本解析，仓库克隆到哪里都能用。
$ProjectDir = $PSScriptRoot
$Script     = Join-Path $ProjectDir "watch_jobs.py"

if (-not (Test-Path $Script)) {
    throw "watch_jobs.py not found next to this script (looked in $ProjectDir)"
}

function Find-Pythonw {
    # Prefer pythonw.exe: it has no console window, so nothing flashes on screen each morning.
    # 优先用 pythonw.exe：没有控制台窗口，每天早上不会闪黑框。
    $candidates = @()
    foreach ($name in @("pythonw", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    foreach ($c in $candidates) {
        $w = Join-Path (Split-Path $c) "pythonw.exe"
        if (Test-Path $w) { return $w }
    }
    if ($candidates.Count -gt 0) { return $candidates[0] }
    return $null
}

if (-not $Python) { $Python = Find-Pythonw }
if (-not $Python) {
    throw "No Python found on PATH. Pass one explicitly, e.g. -Python 'C:\path\to\pythonw.exe'"
}
if (-not (Test-Path $Python)) {
    throw "Python interpreter not found: $Python"
}
if ($Python -notmatch 'pythonw\.exe$') {
    Write-Host "Note: $Python is not pythonw.exe - a console window will flash on each run." -ForegroundColor Yellow
    Write-Host "提示：不是 pythonw.exe，每次运行会闪一下黑框。" -ForegroundColor Yellow
}

Write-Host "Scheduled task to register / 即将注册的计划任务:" -ForegroundColor Cyan
Write-Host "  TaskName   : $TaskName"
Write-Host "  Python     : $Python"
Write-Host "  Script     : $Script"
Write-Host "  WorkingDir : $ProjectDir"
Write-Host "  Trigger    : daily at $RunAt (catches up after a missed run)"
Write-Host ""

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt

# StartWhenAvailable: if the machine was off or asleep at the trigger time,
# the task runs once shortly after the next boot instead of being skipped.
# StartWhenAvailable：到点时电脑关机/休眠错过了，开机后会自动补跑一次。
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

# Runs as the current user, interactively. Deliberately NOT "run whether user is
# logged on or not" - that would require storing the Windows account password.
# 以当前用户交互式运行。刻意不用「不管用户是否登录都运行」——那需要存 Windows 登录密码。
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Task already exists, replacing it... / 已存在同名任务，先注销再重建..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Check predoc.org and NBER RA/pre-doc job pages daily; email any new postings" | Out-Null

Write-Host ""
Write-Host "Registered. / 注册完成:" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime

Write-Host "Run it now  / 手动跑一次: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "Read the log / 查看日志  : Get-Content '$ProjectDir\logs\watch.log' -Tail 20" -ForegroundColor Cyan
