@echo off
rem H2S Mission Control (Windows): double-click or run from cmd.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
if errorlevel 1 pause
