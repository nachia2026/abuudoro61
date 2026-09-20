@echo off
title Kisauni Primary School - Result System
cd /d "%~dp0"

echo ============================================================
echo   KISAUNI PRIMARY SCHOOL - RESULT SYSTEM
echo   Starting the server... please wait.
echo ============================================================
echo.

start "" /b cmd /c "timeout /t 3 >nul && start http://127.0.0.1:5000"

python app.py

echo.
echo ============================================================
echo   The server has stopped. Close this window or press any
echo   key to exit.
echo ============================================================
pause >nul
