@echo off
rem ============================================================================
rem  Energy EMS - remove the startup scheduled task created by
rem  install-autostart-task.bat.  Run as Administrator.
rem  config.json / energy.db / logs are NOT deleted.
rem ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "TASK_NAME=Energy EMS"

net session >nul 2>&1
if errorlevel 1 goto :no_admin

echo [energy-ems] deleting startup task "%TASK_NAME%" ...
schtasks /end /tn "%TASK_NAME%" >nul 2>&1
schtasks /delete /tn "%TASK_NAME%" /f
if errorlevel 1 goto :delete_fail

echo [OK] task removed. Data (config.json / energy.db / logs) is kept.
goto :done

:no_admin
echo [E03] Administrator privileges required.
echo       Right-click this file -^> "Run as administrator".
goto :fail

:delete_fail
echo [E23] schtasks /delete failed - the task may not exist.
echo       List it with: schtasks /query /tn "%TASK_NAME%"
goto :fail

:fail
endlocal
exit /b 1

:done
endlocal
exit /b 0
