@echo off
rem ============================================================================
rem  Energy EMS - uninstall the Windows service (WinSW)
rem  Run as Administrator. config.json / energy.db / logs are NOT deleted.
rem ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 goto :no_admin
if not exist "energy-ems-service.exe" goto :no_exe

echo [energy-ems] stopping and removing service ...
"%~dp0energy-ems-service.exe" stop >nul 2>&1
timeout /t 3 /nobreak >nul
"%~dp0energy-ems-service.exe" uninstall
if errorlevel 1 goto :uninstall_fail

if exist "energy-ems-service.exe" del /f /q "energy-ems-service.exe" >nul 2>&1
echo [OK] service removed. Data (config.json / energy.db / logs) is kept.
goto :done

:no_exe
echo [E11] energy-ems-service.exe not found - service is probably not installed.
goto :fail

:no_admin
echo [E03] Administrator privileges required.
echo       Right-click this file -^> "Run as administrator".
goto :fail

:uninstall_fail
echo [E12] Uninstall failed. Close services.msc and retry, or run:
echo       energy-ems-service.exe uninstall
goto :fail

:fail
endlocal
exit /b 1

:done
endlocal
exit /b 0
