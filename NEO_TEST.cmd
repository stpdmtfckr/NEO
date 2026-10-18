@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--run" goto run
"%SystemRoot%\System32\cmd.exe" /d /e:on /v:off /s /k ""%~f0" --run"
exit /b
:run
title NEO - COMPLETE TEST
chcp 65001 >nul
cd /d "%~dp0"
echo NEO: startup, isolated tests/settings, saved API checks, local voice and ALL atlas languages.
echo Only read-only authenticated API requests are made. They may use API quota.
echo Real atlas actions are blocked; the normal assistant is left running.
echo Wait for SEND_THIS_ZIP at the end. This console will remain open.
if not exist "%~dp0_internal\tools\test_all.py" (
 echo ERROR: extract the entire archive and run this CMD next to NEO.cmd.
 exit /b 2
)
rem Explicit named mode: never fall back to the ordinary launcher.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\launch.ps1" -Mode test-all
set "NEO_TEST_EXIT=%ERRORLEVEL%"
echo.
echo TEST_EXIT_CODE=%NEO_TEST_EXIT%
echo 0=verified full run; 1=findings or unavailable checks; 2=incomplete or missing report.
echo Only SEND_THIS_ZIP from this run identifies the results to send.
exit /b %NEO_TEST_EXIT%
