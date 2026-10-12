@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title ZEUS - Admin ID ^& Password Manager

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0SET_PASSWORD.py" %*
exit /b %ERRORLEVEL%
