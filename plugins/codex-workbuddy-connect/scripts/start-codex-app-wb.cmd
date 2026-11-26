@echo off
rem Launch Codex Desktop with WorkBuddy overrides without changing the
rem machine-wide PowerShell execution policy.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-codex-app-wb.ps1" %*
exit /b %ERRORLEVEL%
