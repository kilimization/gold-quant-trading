@echo off
setlocal
title WhaleFlow launcher

rem ============================================================
rem  WhaleFlow (whale / smart-money) data pipeline launcher
rem    1. skip start when whale_flow.json is already fresh (<180s)
rem    2. pick a pythonw/python that has requests
rem    3. start whale_collector.py in the background (minimized)
rem    4. run whale_health.py for a full link check
rem  Closing this window does NOT stop the collector.
rem  To stop it: end pythonw.exe in Task Manager.
rem ============================================================

set "WF_DIR=C:\Users\kilimy\AppData\Roaming\MetaQuotes\Terminal"
set "WF_JSON=C:\Users\kilimy\AppData\Roaming\MetaQuotes\Terminal\AB75DD8A03E8CC693E1336EB0D50BA2D\MQL4\Files\DWX\whale_flow.json"

if not exist "%WF_DIR%\whale_collector.py" (
  echo [ERROR] not found: %WF_DIR%\whale_collector.py
  pause
  exit /b 1
)

echo.
echo === WhaleFlow collector launcher ===
echo.

rem ---- 1. already fresh? then skip starting a second collector ----
set "WF_FRESH=n"
if exist "%WF_JSON%" (
  for /f %%i in ('powershell -NoProfile -Command "if(((Get-Date)-(Get-Item -LiteralPath $env:WF_JSON).LastWriteTime).TotalSeconds -lt 180){Write-Output 1}else{Write-Output 0}"') do set "WF_FRESH=%%i"
)
if "%WF_FRESH%"=="1" (
  echo [1/3] whale_flow.json is fresh - a collector is already running, skip start.
  goto :health
)
echo [1/3] data is stale or missing - starting collector...

rem ---- 2. locate pythonw ----
set "WF_PY="
for %%p in (pythonw.exe) do if not defined WF_PY set "WF_PY=%%~$PATH:p"
if not defined WF_PY if exist "C:\Users\kilimy\miniconda3\pythonw.exe" set "WF_PY=C:\Users\kilimy\miniconda3\pythonw.exe"
if not defined WF_PY if exist "C:\Users\kilimy\miniconda3\python.exe"  set "WF_PY=C:\Users\kilimy\miniconda3\python.exe"
if not defined WF_PY if exist "D:\Games\VeighHa\pythonw.exe"          set "WF_PY=D:\Games\VeighHa\pythonw.exe"

if not defined WF_PY (
  echo [ERROR] no python found. Install Python 3 with requests:
  echo         python -m pip install requests
  pause
  exit /b 1
)
echo [2/3] interpreter: %WF_PY%

rem ---- 3. start collector ----
start "WhaleCollector" /min "%WF_PY%" "%WF_DIR%\whale_collector.py" --interval 60
echo       collector started (refresh every 60s).

:health
if not defined WF_PY set "WF_PY=python"
echo.
echo [3/3] link check:
echo.
"%WF_PY%" "%WF_DIR%\whale_health.py"
if errorlevel 1 (
  echo [NOTE] check FAILED - see lines marked FAIL above.
) else (
  echo [NOTE] link OK.
)

echo ------------------------------------------------------------
echo  MT4 side - do this once by hand:
echo    drag WhaleFlow_EA onto your BTCUSD / XAUUSD charts
echo    (or drag Test_WhaleParse onto any chart for a read self-test)
echo.
echo  stop collector : end pythonw.exe in Task Manager
echo  autostart at logon:
echo    schtasks /Create /TN WhaleFlowCollector /TR "\"%WF_PY%\" \"%WF_DIR%\whale_collector.py\"" /SC ONLOGON /RL LIMITED /F
echo ------------------------------------------------------------
pause
endlocal
