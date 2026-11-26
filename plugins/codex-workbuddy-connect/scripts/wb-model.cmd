@echo off
rem Switch Codex's global WorkBuddy provider/model and region availability.
rem Usage: wb-model.cmd list ^| current ^| use cn/hy3 ^| hide ai ^| show ai ^| enable ^| disable cn
rem This file must stay GBK(cp936) + CRLF.
setlocal
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wb] Python not found. Install Python 3.10+ or set WORKBUDDY_AI_PYTHON.
    exit /b 1
)
"%PYEXE%" "%~dp0..\bin\wb-model.py" %*
exit /b %ERRORLEVEL%
