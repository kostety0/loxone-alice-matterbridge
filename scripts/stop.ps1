# Остановить Matterbridge (например, перед ручной правкой конфига). PowerShell от администратора.
Stop-ScheduledTask -TaskName Matterbridge -ErrorAction SilentlyContinue
Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "Matterbridge остановлен. Запуск: Start-ScheduledTask -TaskName Matterbridge"
