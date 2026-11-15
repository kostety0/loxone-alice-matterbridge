@echo off
rem Run 1-install.ps1 as administrator
net session >nul 2>&1 || (powershell -Command "Start-Process '%~f0' -Verb RunAs" & exit /b)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp01-install.ps1"
pause
