@echo off
chcp 65001 >nul
title Badminton Booking Manager

echo ========================================
echo   Badminton Booking Manager
echo ========================================
echo.

cd /d "%~dp0"

echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python not found
    pause
    exit /b 1
)

echo Installing dependencies...
pip install -r requirements.txt -q

REM Kill any old instances on port 8000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
timeout /t 1 >nul

echo.
echo Starting web platform (port 8000)...
echo.
echo   Web UI:  http://localhost:8000
echo.
echo Press Ctrl+C to stop
echo ========================================
echo.

python main.py

pause
