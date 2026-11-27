@echo off
rem ============================================================================
rem  Energy EMS - install as a Windows service (WinSW)
rem  Prerequisite: WinSW.exe (v3, x64) placed in this folder.
rem  Run this file as Administrator: right-click -> "Run as administrator".
rem  All messages are ASCII on purpose: cmd.exe misparses UTF-8 batch files.
rem  Chinese documentation: README_DEPLOY.md section 9.
rem ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"

echo [energy-ems] installing Windows service ...
echo.

if not exist "energy_ems.exe" goto :no_exe
if not exist "WinSW.exe" goto :no_winsw

net session >nul 2>&1
if errorlevel 1 goto :no_admin

rem WinSW requires <exe name> to match <xml name>: energy-ems-service.exe/xml
copy /y "WinSW.exe" "energy-ems-service.exe" >nul
if errorlevel 1 goto :copy_fail

"%~dp0energy-ems-service.exe" stop >nul 2>&1
"%~dp0energy-ems-service.exe" uninstall >nul 2>&1
"%~dp0energy-ems-service.exe" install
if errorlevel 1 goto :install_fail

echo [1/3] service registered.
"%~dp0energy-ems-service.exe" start
if errorlevel 1 goto :start_fail
echo [2/3] service started.

echo [3/3] probing http://127.0.0.1:8000/healthz ...
timeout /t 6 /nobreak >nul
set "CODE=0"
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/healthz' -TimeoutSec 8).StatusCode}catch{0}"`) do set "CODE=%%c"
if "%CODE%"=="200" goto :ok

echo.
echo [WARN] /healthz did not return 200 (got: %CODE%).
echo        Check logs\service-*.log and make sure port 8000 is free.
echo        Service may still be starting; wait 30s and open http://127.0.0.1:8000/
goto :done

:ok
echo.
echo [OK] Energy EMS is running as an auto-start Windows service.
echo      Dashboard: http://127.0.0.1:8000/
echo      Logs     : %~dp0logs\service-*.log
echo      Remove   : run uninstall-service.bat as Administrator.
goto :done

:no_exe
echo [E01] energy_ems.exe not found in:
echo       %CD%
echo       Copy this folder's files next to energy_ems.exe, then run again.
goto :fail

:no_winsw
echo [E02] WinSW.exe not found.
echo       Download WinSW v3 x64 from https://github.com/winsw/winsw/releases
echo       and save it as WinSW.exe in this folder.
echo       No internet on site? Use install-autostart-task.bat instead (no extra file).
goto :fail

:no_admin
echo [E03] Administrator privileges required.
echo       Right-click this file -^> "Run as administrator".
goto :fail

:copy_fail
echo [E04] Failed to copy WinSW.exe to energy-ems-service.exe.
echo       The file may be locked by a previous install. Stop the service first.
goto :fail

:install_fail
echo [E05] Service registration failed. Check that .NET Framework 4.6.1+ is present.
goto :fail

:start_fail
echo [E06] Service could not start. See logs\service-*.log for the reason.
goto :fail

:fail
endlocal
exit /b 1

:done
endlocal
exit /b 0
