# Шаг 3. Настройка плагина matterbridge-loxone: адрес, пользователь, пароль и список устройств.
# Запуск (PowerShell от администратора):
#   powershell -ExecutionPolicy Bypass -File .\3-configure-loxone.ps1 -LoxoneHost 192.168.1.77 -Port 80 -User alice
# Пароль спрашивается скрытым вводом и никуда не выводится.
param(
    [Parameter(Mandatory)] [string]$LoxoneHost,
    [int]$Port = 80,
    [Parameter(Mandatory)] [string]$User
)
$ErrorActionPreference = 'Stop'
function Say($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }

# Matterbridge перезаписывает конфиг при выходе - правим только остановленный
Say "Останавливаю Matterbridge"
Stop-ScheduledTask -TaskName Matterbridge -ErrorAction SilentlyContinue
Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 2

$sec = Read-Host "Пароль пользователя Loxone '$User'" -AsSecureString
$Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

# --- Структура Miniserver'а --------------------------------------------------
Say "Читаю список устройств с http://${LoxoneHost}:$Port"
$auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${User}:$Password"))
try {
    $r = Invoke-WebRequest "http://${LoxoneHost}:$Port/data/LoxAPP3.json" -Headers @{Authorization = "Basic $auth" } -UseBasicParsing
} catch {
    throw "Не удалось получить структуру Miniserver'а: $($_.Exception.Message). Проверьте адрес, порт и пароль. Внимание: после ~10 неудачных входов Loxone временно блокирует IP."
}
# PowerShell 5.1 неверно угадывает кодировку - декодируем UTF-8 сами
$s = [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json

$list = New-Object System.Collections.Generic.List[string]
foreach ($p in $s.controls.PSObject.Properties) {
    $ctl = $p.Value; $room = $s.rooms.($ctl.room).name
    switch ($ctl.type) {
        'Switch'   { $list.Add("$($p.Name),light"); Write-Host "  [$room] $($ctl.name) -> light" }
        'Jalousie' { $list.Add("$($p.Name),shade"); Write-Host "  [$room] $($ctl.name) -> shade" }
        'LightControllerV2' {
            foreach ($sp in $ctl.subControls.PSObject.Properties) {
                if ($sp.Value.type -in 'Switch', 'Dimmer', 'ColorPickerV2') {
                    $list.Add("$($sp.Name),lightoutput")
                    Write-Host "  [$room] $($ctl.name) / $($sp.Value.name) -> lightoutput"
                }
            }
        }
    }
}
if ($list.Count -eq 0) {
    throw "Устройств не найдено. Выдайте пользователю '$User' права на нужные блоки в Loxone Config и сохраните в Miniserver."
}
Write-Host "Найдено устройств: $($list.Count)"

# --- Конфиг плагина ----------------------------------------------------------
$dir = Join-Path $env:USERPROFILE '.matterbridge'
$f = Join-Path $dir 'matterbridge-loxone.config.json'
New-Item -ItemType Directory -Force $dir | Out-Null
if (Test-Path $f) {
    $c = ([IO.File]::ReadAllText($f)).TrimStart([char]0xFEFF) | ConvertFrom-Json
} else {
    $c = [pscustomobject]@{ name = 'matterbridge-loxone'; type = 'DynamicPlatform' }
}
$values = [ordered]@{
    host = $LoxoneHost; port = $Port; username = $User; password = $Password
    uuidsandtypes = [string[]]$list.ToArray()
}
foreach ($k in $values.Keys) {
    $c.PSObject.Properties.Remove($k)
    $c | Add-Member -NotePropertyName $k -NotePropertyValue $values[$k]
}
# Строго UTF-8 БЕЗ BOM: с BOM Matterbridge не читает JSON ("Unexpected token")
$json = $c | ConvertTo-Json -Depth 10
[IO.File]::WriteAllText($f, $json, (New-Object System.Text.UTF8Encoding $false))
$Password = $null; $auth = $null

# --- Проверка ----------------------------------------------------------------
$chk = [IO.File]::ReadAllText($f) | ConvertFrom-Json
Say "Записано: host=$($chk.host) port=$($chk.port) user=$($chk.username) устройств=$(@($chk.uuidsandtypes).Count)"
Start-ScheduledTask -TaskName Matterbridge -ErrorAction SilentlyContinue
Say "Готово. Matterbridge запущен - проверьте вкладки Logs и Devices в веб-интерфейсе"
