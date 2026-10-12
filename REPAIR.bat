@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ZEUS - REPAIR

echo.
echo ====================================================================
echo   ZEUS - SYSTEM ^& FILE REPAIR
echo ====================================================================
echo.
echo   This utility diagnoses and repairs ZEUS files, databases,
echo   configurations, runtime states, packages, and models:
echo     - Backs up all databases and config files to backups\repair_*\
echo     - Stops stuck background worker processes ^& cleans stale state
echo     - Restores any missing configuration sections/keys in config.ini
echo     - Initializes ^& repairs SQLite databases and search indexes
echo     - Regenerates firewall scripts for the current folder path
echo     - Reinstalls any missing Python packages ^& verifies models/voice
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :have_venv

echo   [1/3] Virtual environment (.venv) is missing - rebuilding .venv...
set "SYS_PY="
for %%C in ("py -3.12" "py -3.11" "py -3" "python") do (
    if not defined SYS_PY (
        %%~C -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
        if not errorlevel 1 set "SYS_PY=%%~C"
    )
)
if not defined SYS_PY (
    echo.
    echo   [FAIL] Python 3.11 or 3.12 was not found on this machine.
    echo          Please install Python 3.12 or run "STEP 1 : INSTALL".
    if /i not "%~1"=="--auto" pause
    exit /b 1
)
%SYS_PY% -m venv .venv
if not exist "%PY%" (
    echo   [FAIL] Could not recreate .venv.
    if /i not "%~1"=="--auto" pause
    exit /b 1
)

:have_venv
echo   [1/3] Running diagnostic ^& safe repair pass (agni_ops repair)...
"%PY%" -m agni_ops repair
set "OPS_CODE=%ERRORLEVEL%"

echo.
echo   [2/3] Checking ^& repairing packages, models, databases, and readiness files...
"%PY%" "%~dp0scripts\install_everything.py" --repair --skip-voice
set "INST_CODE=%ERRORLEVEL%"

echo.
echo   [3/3] Running post-repair preflight verification...
"%PY%" -m agni_ops preflight
set "PRE_CODE=%ERRORLEVEL%"

echo.
echo ====================================================================
if "%PRE_CODE%"=="0" (
    echo   [REPAIR COMPLETE] All essential files, databases, and packages
    echo                     are healthy. You can now run "STEP 2 : START".
) else (
    echo   [REPAIR FINISHED] Some components still need attention ^(code %PRE_CODE%^).
    echo                     Review the messages above or run "STEP 1 : INSTALL".
)
echo ====================================================================
echo.
if /i not "%~1"=="--auto" pause
exit /b %PRE_CODE%
