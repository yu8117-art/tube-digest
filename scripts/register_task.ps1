# Windows 작업 스케줄러에 "N분마다 tubedigest run" 작업을 등록한다.
# 사용: powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 [-Minutes 15]
# 해제: Unregister-ScheduledTask -TaskName TubeDigest -Confirm:$false
param(
    [int]$Minutes = 15,
    [string]$TaskName = "TubeDigest"
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\pythonw.exe"   # pythonw: 실행할 때 콘솔 창이 뜨지 않음
if (-not (Test-Path $python)) { throw "가상환경이 없음: $python (README의 설치 단계를 먼저 진행)" }

$action   = New-ScheduledTaskAction -Execute $python -Argument "-m tubedigest run" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "유튜브 새 영상 요약 기사 발행 + 폰 알림" -Force | Out-Null
Write-Host "등록 완료: '$TaskName' ($Minutes 분마다). 로그: $root\logs\digest.log"
