@echo off
rem Shared Python discovery for Codex WorkBuddy launchers.
rem This file is intentionally ASCII and uses CRLF line endings.
set "PYEXE="
if defined WORKBUDDY_AI_PYTHON if exist "%WORKBUDDY_AI_PYTHON%" set "PYEXE=%WORKBUDDY_AI_PYTHON%"
if not defined PYEXE if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" set "PYEXE=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python313\python.exe" set "PYEXE=%ProgramFiles%\Python313\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python312\python.exe" set "PYEXE=%ProgramFiles%\Python312\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python311\python.exe" set "PYEXE=%ProgramFiles%\Python311\python.exe"
if not defined PYEXE for /f "delims=" %%i in ('where python 2^>NUL') do if not defined PYEXE set "PYEXE=%%i"

set "PYWDIR="
set "PYWEXE="
if defined PYEXE for %%d in ("%PYEXE%") do set "PYWDIR=%%~dpd"
if defined PYWDIR if exist "%PYWDIR%pythonw.exe" set "PYWEXE=%PYWDIR%pythonw.exe"
if not defined PYWEXE set "PYWEXE=%PYEXE%"
exit /b 0
