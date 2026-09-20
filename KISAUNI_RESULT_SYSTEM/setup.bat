@echo off
title Kisauni Result System - Setup
cd /d "%~dp0"

echo ============================================================
echo   KISAUNI PRIMARY SCHOOL - RESULT SYSTEM
echo   One-time setup: installing required packages...
echo   (This only needs to be run ONCE. It may take a few
echo    minutes depending on your internet speed.)
echo ============================================================
echo.

pip install -r requirements.txt

echo.
echo ============================================================
echo   Setup complete! From now on, just double-click run.bat
echo   to start the system.
echo ============================================================
pause
