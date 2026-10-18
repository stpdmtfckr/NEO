@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NEO
if /i "%~1"=="--install-voices" goto install_voices
if /i "%~1"=="--install-deps" goto install_deps
if /i "%~1"=="--cancel-ollama" goto cancel_ollama
if not "%~1"=="" goto diagnostics
if not exist "%~dp0NEO.exe" (
  echo NEO.exe is missing. Extract the entire NEO archive before launching.
  pause
  exit /b 2
)
rem Ollama installation is automatic. Only server autostart is user-switchable.
rem This helper runs hidden, respects settings before start, and never pulls models.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\start_ollama_background.ps1"
call :ensure_deps
if errorlevel 1 (
  echo Dependencies could not be installed. Run "NEO.cmd --install-deps" to see the details.
  pause
  exit /b 1
)
rem Voice model: only when the voice is turned on in Settings (tier from Settings). No hidden downloads.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\prepare_response_voice.ps1"
rem The GUI shim owns setup, readiness and visible error reporting. Detach this
rem batch so its console does not remain attached to a running desktop session.
start "" "%~dp0NEO.exe"
exit /b %ERRORLEVEL%

:install_voices
call :ensure_deps
if errorlevel 1 exit /b 1
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\prepare_response_voice.ps1" -Restore
exit /b %ERRORLEVEL%

:install_deps
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\start_ollama_background.ps1"
call :deps
exit /b %ERRORLEVEL%

:cancel_ollama
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\start_ollama_background.ps1" -Cancel
exit /b %ERRORLEVEL%

:ensure_deps
if exist "%LOCALAPPDATA%\NeoJarvis\bootstrap\deps184.ok" exit /b 0
call :deps
exit /b %ERRORLEVEL%

:deps
set "NEO_HELPER=powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\installer_helper.ps1""
echo [1/2] Python 3.12 and NEO packages...
%NEO_HELPER% -Action runtime
if errorlevel 1 exit /b 1
echo [2/2] NEO command model (local check)...
%NEO_HELPER% -Action neo_model
if errorlevel 1 exit /b 1
if not exist "%LOCALAPPDATA%\NeoJarvis\bootstrap" mkdir "%LOCALAPPDATA%\NeoJarvis\bootstrap"
echo 184> "%LOCALAPPDATA%\NeoJarvis\bootstrap\deps184.ok"
echo NEO dependencies are ready. Download optional models from Settings when needed.
exit /b 0

:diagnostics
if not "%~2"=="" (
  echo NEO diagnostics accept one mode. Run NEO.cmd --help.
  exit /b 2
)
set "NEO_MODE=%~1"
set "NEO_MODE=%NEO_MODE:--=%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\launch.ps1" -Mode "%NEO_MODE%"
set "NEO_EXIT=%ERRORLEVEL%"
if /i "%~1"=="--test-all" (
  if "%NEO_EXIT%"=="1" (
    echo NEO test completed with findings. See the final SEND_THIS_ZIP above.
    exit /b 1
  )
)
if not "%NEO_EXIT%"=="0" (
  echo.
  echo NEO could not complete this operation. See the diagnostic message above.
  if "%~1"=="" pause
)
exit /b %NEO_EXIT%
