# 注册 / 删除 watchlist PE 分位表的每周定时任务（Windows 任务计划程序，当前用户）。
#
#   powershell -ExecutionPolicy Bypass -File scripts\pe_rank_weekly.ps1              # 周六 08:00
#   powershell -ExecutionPolicy Bypass -File scripts\pe_rank_weekly.ps1 -Day Sunday -At 09:30
#   powershell -ExecutionPolicy Bypass -File scripts\pe_rank_weekly.ps1 -Unregister
#
# 时点按本机时区。布里斯班无夏令时：周六 08:00 = 美东周五 18:00（EDT）/ 17:00（EST），
# 两季都在收盘后——盘中跑 yfinance 末根是未收盘 K 线。
# conhost --headless：不弹黑窗口挂 5 分钟；StartWhenAvailable：到点时电脑没开/没登录，
# 下次可用时补跑。输出追加到 reports\pe_rank\scheduled.log，结果照常写 reports\pe_rank\。
param(
    [string]$At = "08:00",
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$Day = "Saturday",
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$Name = "sec-downloader pe_rank weekly"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Output "已删除定时任务：$Name"
    return
}

$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Log = Join-Path $Root "reports\pe_rank\scheduled.log"
if (-not (Test-Path $Py)) { throw "找不到 venv 的 python：$Py" }
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

# 每次运行先写一行时间戳，出问题时 log 里能对上是哪一周
$inner = "echo ==== %DATE% %TIME% ==== >> `"$Log`" && `"$Py`" valuation\pe_rank.py >> `"$Log`" 2>&1"
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless cmd.exe /c `"$inner`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
    -Description "每周跑 valuation/pe_rank.py：watchlist 当前 PE 的历史分位 + 前瞻 PE（sec-filing-downloader）" `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName $Name
Write-Output ("已注册：{0}，每周{1} {2}（本机时区），下次运行 {3}" -f $Name, $Day, $At,
    (Get-ScheduledTaskInfo -TaskName $Name).NextRunTime)
