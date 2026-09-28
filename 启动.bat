@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo       EPUB 工具箱
echo ==========================================
echo.

where py >nul 2>nul
if not errorlevel 1 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
    ) else (
        echo Python was not found.
        echo Please install Python 3.6 or newer and enable "Add Python to PATH".
        pause
        exit /b 1
    )
)

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,6) else 1)"
if errorlevel 1 (
    echo Python 3.6 or newer is required.
    pause
    exit /b 1
)

echo Checking/installing dependencies...
%PY% -m pip install Flask beautifulsoup4
if errorlevel 1 (
    echo.
    echo Dependency installation failed.
    echo Please check your Python and network configuration.
    pause
    exit /b 1
)

echo.
echo Starting server...
start "" "http://127.0.0.1:5000/"
%PY% app.py

endlocal
