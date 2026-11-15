# Шаг 2. Автозапуск Matterbridge в фоне (без окна, при включении ПК, без входа в Windows).
# Запуск (PowerShell от администратора):
#   powershell -ExecutionPolicy Bypass -File .\2-autostart.ps1 -LoxoneHost 192.168.1.77
# Сетевой интерфейс для Matter (mDNS) определяется автоматически по подсети Miniserver'а,
# либо задайте явно: -Interface "Ethernet"
param(
    [string]$LoxoneHost,
    [string]$Interface
)
$ErrorActionPreference = 'Stop'
function Say($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }

if (-not $Interface) {
    if (-not $LoxoneHost) { throw "Укажите -LoxoneHost или -Interface" }
    $prefix = ($LoxoneHost -split '\.')[0..2] -join '.'
    $Interface = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "$prefix.*" } |
        Select-Object -First 1).InterfaceAlias
    if (-not $Interface) { throw "Не нашёл сетевой адаптер в подсети $prefix.x - задайте -Interface вручную" }
}
Say "Интерфейс для Matter: $Interface"

$mb = Join-Path $env:APPDATA 'npm\matterbridge.cmd'
if (-not (Test-Path $mb)) { throw "Не найден $mb - сначала запустите 1-install.ps1" }

# Останавливаем всё, чтобы не было двух копий (две копии = двойные попытки входа = блокировка IP в Loxone)
Stop-ScheduledTask -TaskName Matterbridge -ErrorAction SilentlyContinue
Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force

# Обёртка: если Matterbridge завершился (в т.ч. по кнопке Restart в веб-интерфейсе) - запускаем снова
$run = Join-Path $env:USERPROFILE '.matterbridge\run.ps1'
New-Item -ItemType Directory -Force (Split-Path $run) | Out-Null
$wrapper = @"
while (`$true) {
    & '$mb' -bridge -mdnsinterface '$Interface'
    Start-Sleep -Seconds 5
}
"@
[IO.File]::WriteAllText($run, $wrapper, (New-Object System.Text.UTF8Encoding $false))

Unregister-ScheduledTask -TaskName Matterbridge -Confirm:$false -ErrorAction SilentlyContinue
$action    = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$run`""
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName Matterbridge -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Start-ScheduledTask -TaskName Matterbridge
Start-Sleep 3
Get-ScheduledTask -TaskName Matterbridge | Select-Object TaskName, State | Format-Table -AutoSize
$ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $Interface | Select-Object -First 1).IPAddress
Say "Готово. Веб-интерфейс: http://${ip}:8283 (через 20-30 секунд)"
