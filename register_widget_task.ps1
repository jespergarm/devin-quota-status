$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptDir 'devin_quota_widget.pyw'

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($python) {
        $candidate = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
        if (Test-Path $candidate) { $pythonw = $candidate }
    }
}
if (-not $pythonw) {
    throw 'pythonw.exe was not found. Install Python 3 or add it to PATH first.'
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$target`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Devin Quota Strip' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output 'Registered scheduled task: Devin Quota Strip'
