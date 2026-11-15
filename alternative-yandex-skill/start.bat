@echo off
rem Запуск моста Loxone -> Алиса и туннеля. Замените ВАШ-ПОДДОМЕН на свой.
chcp 65001 >nul
cd /d "%~dp0"
start "Loxone-Alice bridge" /min cmd /k python server.py config.toml
start "Tunnel" /min cmd /k tuna http 8090 --subdomain=ВАШ-ПОДДОМЕН
