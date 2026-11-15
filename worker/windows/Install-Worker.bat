@echo off
chcp 65001 >nul
title MediaScan worker - installation
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-worker.ps1"
echo.
pause
