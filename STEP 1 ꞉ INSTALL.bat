@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ZEUS - STEP 1 : INSTALL (Full Online ^& Offline Setup)

echo.
echo ====================================================================
echo   ZEUS - STEP 1 : INSTALL (FULL ONLINE ^& OFFLINE SETUP)
echo ====================================================================
echo.
echo   This installer sets up and verifies the complete ZEUS
echo   environment (both Online and Offline components):
echo     [1] Python virtual environment (.venv) ^& core packages
echo     [2] Hardware profile ^& local AI model plan
echo     [3] Local Ollama LLM ^& vision models
echo     [4] Offline Whisper speech-to-text engine
echo     [5] Offline InsightFace authentication ^& liveness models
echo     [6] Local Chatterbox voice synthesis engine
echo     [7] Offline ^& Online databases, RAG knowledge pack ^& verification
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :have_venv

echo   [STEP 1.1] Creating Python virtual environment (.venv)...
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
    echo          Install Python 3.12 (64-bit) from https://www.python.org/downloads/windows/
    echo          and tick "Add python.exe to PATH", then run STEP 1 : INSTALL again.
    echo.
    pause
    exit /b 1
)

echo          Using system Python: %SYS_PY%
%SYS_PY% -m venv .venv
if not exist "%PY%" (
    echo.
    echo   [FAIL] Could not create .venv in "%~dp0".
    echo          Check folder permissions and available disk space.
    echo.
    pause
    exit /b 1
)

:have_venv
echo   [OK]   Virtual environment ready: %PY%

set "REQ_FILE=%~dp0config\requirements.txt"
if not exist "%REQ_FILE%" set "REQ_FILE=%~dp0requirements.txt"

:: If an offline wheels cache folder exists, install from it first before running full setup
if exist "%~dp0wheels\*.whl" if exist "%REQ_FILE%" (
    echo.
    echo   [OFFLINE CACHE] Found local wheels in .\wheels - installing cached packages...
    "%PY%" -m pip install --no-index --find-links="%~dp0wheels" -r "%REQ_FILE%" >nul 2>nul
)

echo.
echo   [STEP 1.2] Running full Online ^& Offline installation...
"%PY%" "%~dp0scripts\install_everything.py" %*
set "INSTALL_CODE=%ERRORLEVEL%"

echo.
echo   [STEP 1.3] Verifying Online ^& Offline installation state...
"%PY%" -m agni_ops install-verify
set "VERIFY_CODE=%ERRORLEVEL%"

echo.
echo ====================================================================
if "%INSTALL_CODE%"=="0" (
    echo   [COMPLETE] ZEUS installation finished successfully.
    echo   Next step: Run "STEP 2 : START" to launch ZEUS.
) else (
    echo   [FINISHED WITH WARNINGS] Core setup finished ^(code %INSTALL_CODE%^).
    echo   You can start ZEUS with "STEP 2 : START" or run "REPAIR.bat"
    echo   anytime to retry optional components.
)
echo ====================================================================
echo.
pause
exit /b %INSTALL_CODE%
