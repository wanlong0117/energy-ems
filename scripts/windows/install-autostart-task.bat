@echo off
rem ============================================================================
rem  Energy EMS - auto-start at boot WITHOUT WinSW / NSSM
rem  Uses a Windows Scheduled Task (runs as SYSTEM at startup).
rem  Run as Administrator.
rem
rem  Difference from the service route:
rem    - no third-party binary needed, works fully offline on site
rem    - Windows Task Scheduler retries a failed start only if the machine reboots;
rem      it does NOT restart a crashed process like a service does.
rem  If you need crash recovery, use install-service.bat with WinSW instead.
rem ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "TASK_NAME=Energy EMS"
set "EXE=%~dp0energy_ems.exe"

if not exist "%EXE%" goto :no_exe
net session >nul 2>&1
if errorlevel 1 goto :no_admin

echo [energy-ems] registering startup task "%TASK_NAME%" ...
schtasks /create /tn "%TASK_NAME%" /tr "\"%EXE%\"" /sc onstart /delay 0000:30 /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 goto :create_fail

echo [1/2] task registered (runs 30s after boot, as SYSTEM).
schtasks /run /tn "%TASK_NAME%"
if errorlevel 1 goto :run_fail
echo [2/2] task triggered.

echo.
echo [OK] Dashboard: http://127.0.0.1:8000/
echo      Verify : schtasks /query /tn "%TASK_NAME%"
echo      Remove : run uninstall-autostart-task.bat as Administrator.
echo.
echo [NOTE] A scheduled task is not a service: it does not restart a crashed
echo        process. For 7x24 unattended site use install-service.bat (WinSW).
goto :done

:no_exe
echo [E01] energy_ems.exe not found in:
echo       %CD%
goto :fail

:no_admin
echo [E03] Administrator privileges required.
echo       Right-click this file -^> "Run as administrator".
goto :fail

:create_fail
echo [E21] schtasks /create failed. Check Task Scheduler service is running.
goto :fail

:run_fail
echo [E22] Task registered but could not be started now; it will run at next boot.
goto :fail

:fail
endlocal
exit /b 1

:done
endlocal
exit /b 0
