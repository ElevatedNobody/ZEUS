@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ZEUS - STEP 2 : START

echo.
echo ====================================================================
echo   ZEUS - STEP 2 : START
echo ====================================================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
set "INSTALL_BAT="
for %%F in ("%~dp0STEP 1*INSTALL.bat") do set "INSTALL_BAT=%%~fF"
set "REPAIR_BAT=%~dp0REPAIR.bat"

set "MAIN_PY=%~dp0User Editable Files\main.py"
if not exist "%MAIN_PY%" set "MAIN_PY=%~dp0main.py"

:: ------------------------------------------------------------------
:: 1. Check whether ZEUS is installed (.venv, core files, packages, DBs)
:: ------------------------------------------------------------------
echo   [1/3] Checking installation status...

if not exist "%PY%" (
    echo.
    echo   [NOT INSTALLED] Python virtual environment ^(.venv^) was not found.
    goto :offer_install
)

if not exist "%MAIN_PY%" (
    echo.
    echo   [ERROR] main.py is missing from "User Editable Files\main.py".
    pause
    exit /b 1
)

if not exist "%~dp0config.ini" (
    echo.
    echo   [WARN] config.ini is missing - running REPAIR.bat to restore configuration...
    if exist "%REPAIR_BAT%" call "%REPAIR_BAT%" --auto
)

:: Verify core Python packages import cleanly
"%PY%" -c "import PySide6, requests, psutil" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [NOT INSTALLED] Core Python packages ^(PySide6, requests, psutil^) are missing.
    goto :offer_install
)

echo   [OK]   Core environment and packages are installed.

:: ------------------------------------------------------------------
:: 2. Start local model runtime and run preflight verification
:: ------------------------------------------------------------------
echo.
echo   [2/3] Starting local runtime and verifying all components ^(Preflight^)...
"%PY%" "%~dp0scripts\start_check.py"
"%PY%" -m agni_ops preflight
if errorlevel 1 (
    echo.
    echo   [WARN] Preflight detected an issue with an essential component.
    if exist "%REPAIR_BAT%" (
        set /p "DO_REPAIR=  Would you like to run REPAIR.bat now to fix it? [Y/N]: "
        if /i "!DO_REPAIR!"=="Y" (
            call "%REPAIR_BAT%" --auto
            "%PY%" -m agni_ops preflight
            if errorlevel 1 (
                echo.
                echo   [FAIL] ZEUS still cannot start. Please run "STEP 1 : INSTALL" or check logs\.
                pause
                exit /b 1
            )
        ) else (
            pause
            exit /b 1
        )
    ) else (
        pause
        exit /b 1
    )
)

:: ------------------------------------------------------------------
:: 3. Launch ZEUS
:: ------------------------------------------------------------------
echo.
echo   [3/3] Starting ZEUS...
echo.
"%PY%" "%MAIN_PY%" --voice %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo   [ERROR] ZEUS exited with code %CODE%.
    echo           Check logs\crash.log or run REPAIR.bat to diagnose and fix issues.
    echo.
    pause
)
exit /b %CODE%

:offer_install
echo.
echo   ZEUS is not fully installed yet.
if defined INSTALL_BAT if exist "!INSTALL_BAT!" (
    set /p "RUN_INSTALL=  Would you like to run STEP 1 : INSTALL now? [Y/N]: "
    if /i "!RUN_INSTALL!"=="Y" (
        call "!INSTALL_BAT!"
        if exist "%PY%" (
            echo.
            echo   Installation completed. Continuing to start ZEUS...
            goto :eof_restart
        )
    )
) else (
    echo   Please run "STEP 1 : INSTALL" first.
)
echo.
pause
exit /b 1

:eof_restart
"%PY%" "%~dp0scripts\start_check.py"
"%PY%" -m agni_ops preflight
if not errorlevel 1 "%PY%" "%MAIN_PY%" --voice %*
exit /b %ERRORLEVEL%
