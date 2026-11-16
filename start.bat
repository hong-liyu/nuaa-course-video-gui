@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title NUAA Course Video Web Studio

set "PY_BIN="

if exist "%USERPROFILE%\.cache\nuaa-course-video\venv\Scripts\python.exe" (
    set "PY_BIN=%USERPROFILE%\.cache\nuaa-course-video\venv\Scripts\python.exe"
    goto :run
)

for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY_BIN (
        echo "%%P" | findstr /i "WindowsApps" >nul || (
            "%%P" --version >nul 2>nul && set "PY_BIN=%%P"
        )
    )
)
if defined PY_BIN goto :run

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 -c "import sys" >nul 2>nul
    if %errorlevel% equ 0 (
        py -3 "%~dp0gui.py" %*
        goto :check_err
    )
)

for %%H in ("HKCU\Software\Python\PythonCore" "HKLM\Software\Python\PythonCore") do (
    for /f "tokens=*" %%K in ('reg query %%~H 2^>nul') do (
        for /f "tokens=2,*" %%A in ('reg query "%%K\InstallPath" /ve 2^>nul ^| findstr /i "REG_SZ"') do (
            if exist "%%B\python.exe" if not defined PY_BIN set "PY_BIN=%%B\python.exe"
        )
        for /f "tokens=2,*" %%A in ('reg query "%%K\InstallPath" /v ExecutablePath 2^>nul ^| findstr /i "REG_SZ"') do (
            if exist "%%B" if not defined PY_BIN set "PY_BIN=%%B"
        )
    )
)
if defined PY_BIN goto :run

for %%D in (
    "%USERPROFILE%\miniconda3"
    "%USERPROFILE%\anaconda3"
    "%LOCALAPPDATA%\miniconda3"
    "%LOCALAPPDATA%\anaconda3"
    "%ProgramData%\miniconda3"
    "%ProgramData%\anaconda3"
) do (
    if exist "%%~D\python.exe" if not defined PY_BIN set "PY_BIN=%%~D\python.exe"
)
if defined PY_BIN goto :run

echo [ERROR] Python 3.9+ not found. Please install Python 3 or add it to PATH.
pause
exit /b 1

:run
"%PY_BIN%" "%~dp0gui.py" %*

:check_err
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Process exited with code %errorlevel%.
    pause
    exit /b %errorlevel%
)
