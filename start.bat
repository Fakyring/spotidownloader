@echo off
setlocal
cd /d "%~dp0" || goto :fail

for %%F in (app.py launch.py requirements.txt templates\index.html static\granite.css static\granite.js) do (
    if not exist "%%F" (
        echo Missing %%F. Run start.bat from the complete project folder.
        goto :fail
    )
)

set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Creating Python environment...
    py -3 --version >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv || goto :fail
    ) else (
        python --version >nul 2>&1
        if errorlevel 1 (
            echo Python not found. Install Python 3 from python.org.
            goto :fail
        )
        python -m venv .venv || goto :fail
    )
)

"%VENV_PY%" -c "import flask, requests, yt_dlp, yt_dlp_ejs" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    "%VENV_PY%" -m pip install -r requirements.txt || goto :fail
)

"%VENV_PY%" launch.py
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Startup failed. See the error above.
echo If port 8765 is in use, close the previous server window and try again.
pause
exit /b 1
