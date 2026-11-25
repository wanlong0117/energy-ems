@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Win11 双击启动脚本：定位 onedir 包内的 energy_ems.exe 并以其目录为工作目录。
rem config.json 与 energy.db 存放在 exe 同级目录（升级替换 exe 不丢数据）。
set "EXE=%~dp0energy_ems\energy_ems.exe"
if not exist "%EXE%" set "EXE=%~dp0energy_ems.exe"
if not exist "%EXE%" (
  echo [ERROR] energy_ems.exe not found. Please keep this bat with the onedir package.
  pause
  exit /b 1
)

for %%F in ("%EXE%") do cd /d "%%~dpF"

echo Starting Energy EMS...
echo Config and database directory: %CD%
start "Energy EMS" "%EXE%"
exit /b 0
