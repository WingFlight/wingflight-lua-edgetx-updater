@echo off
REM Wingflight Lua EdgeTX/OpenTX Updater Launcher

echo ========================================
echo Wingflight EdgeTX/OpenTX Updater
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.7 or higher from python.org
    echo.
    pause
    exit /b 1
)

echo Python found!
echo.
echo Starting updater...
echo.

pythonw update_radio_gui.py

if errorlevel 1 (
    echo.
    echo ERROR: Failed to start updater
    pause
    exit /b 1
)

exit /b 0
