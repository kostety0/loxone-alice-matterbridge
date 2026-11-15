# Шаг 1. Установка Node.js и Matterbridge + подготовка сети Windows.
# Запуск: двойной щелчок по Install.bat (сам попросит права администратора)
#   или:  powershell -ExecutionPolicy Bypass -File .\1-install.ps1   (PowerShell от администратора)
$ErrorActionPreference = 'Stop'
function Say($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Запустите PowerShell от имени администратора" }

# --- 1. Node.js (LTS) ------------------------------------------------------
Say "Проверяю Node.js"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    # winget есть не везде (LTSC, старые сборки) - качаем MSI напрямую
    Say "Node.js не найден - скачиваю LTS с nodejs.org"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'
    $idx = Invoke-RestMethod 'https://nodejs.org/dist/index.json'
    $lts = ($idx | Where-Object { $_.lts } | Select-Object -First 1).version
    $msi = Join-Path $env:TEMP "node-$lts-x64.msi"
    Invoke-WebRequest "https://nodejs.org/dist/$lts/node-$lts-x64.msi" -OutFile $msi -UseBasicParsing
    Say "Устанавливаю Node.js $lts"
    Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
}
Write-Host "Node.js $(node --version)"

# --- 2. Matterbridge (плагин ставится из веб-интерфейса) ---------------------------------------
Say "Устанавливаю matterbridge (1-3 минуты)"
npm install -g matterbridge --omit=dev
if ($LASTEXITCODE -ne 0) { throw "npm install завершился с ошибкой" }

# --- 3. Сеть: брандмауэр, частный профиль, Teredo ---------------------------
Say "Разрешаю Node.js в брандмауэре (частная сеть)"
$node = (Get-Command node).Source
Get-NetFirewallRule -DisplayName 'Matterbridge*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'Matterbridge (node TCP)' -Program $node -Direction Inbound -Action Allow -Protocol TCP -Profile Private | Out-Null
New-NetFirewallRule -DisplayName 'Matterbridge (node UDP)' -Program $node -Direction Inbound -Action Allow -Protocol UDP -Profile Private | Out-Null

foreach ($p in Get-NetConnectionProfile) {
    if ($p.NetworkCategory -eq 'Public') {
        Say "Сеть '$($p.Name)' - 'Общедоступная', делаю 'Частной' (нужно для Matter/mDNS)"
        Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private
    }
}

Say "Отключаю Teredo (мешает Matter выбрать правильный интерфейс)"
netsh interface teredo set state disabled | Out-Null

# --- 4. Не засыпать ----------------------------------------------------------
powercfg /change standby-timeout-ac 0

Say "Готово. Запустите Matterbridge (2-autostart.ps1) и установите плагин matterbridge-loxone в веб-интерфейсе"
