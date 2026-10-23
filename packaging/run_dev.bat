@echo off
rem ===========================================================================
rem  QingFeng Cleaner / QingFeng Cleaner - development launcher
rem  Runs main.py with the bundled Python 3.12 (falls back to python on PATH).
rem
rem  Usage:
rem    packaging\run_dev.bat                :: start GUI with tray
rem    packaging\run_dev.bat --scan-only    :: scan and print report only
rem    packaging\run_dev.bat --no-tray      :: no system tray
rem    packaging\run_dev.bat --hidden       :: start hidden in tray
rem    packaging\run_dev.bat --rules-dir D:\my-rules
rem    packaging\run_dev.bat --version
rem ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>nul

rem --- make project root the working directory (script lives in packaging\) ---
pushd "%~dp0.." >nul 2>nul
if errorlevel 1 (
    echo [run_dev] ERROR: cannot enter project root.
    exit /b 1
)

rem --- locate a Python 3.12 interpreter -------------------------------------
set "DSH_PY=C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
set "PY=%DSH_PY%"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo  QingFeng Cleaner / QingFeng Cleaner - dev launcher
echo  project : %CD%
echo  python  : %PY%
echo  args    : %*
echo ============================================================
echo.

"%PY%" -c "import sys; print('Python ' + sys.version.split()[0] + ' @ ' + sys.executable)"
if errorlevel 1 (
    echo.
    echo [run_dev] ERROR: no usable Python 3.12 found.
    echo [run_dev] bundled interpreter expected at:
    echo [run_dev]   %DSH_PY%
    echo [run_dev] install Python 3.12 ^(with tkinter^) and put "python" on PATH.
    popd >nul 2>nul
    exit /b 1
)

"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [run_dev] WARNING: this Python has no tkinter - GUI mode will fail.
    echo [run_dev]          --scan-only mode still works.
    echo.
)

"%PY%" main.py %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [run_dev] finished, exit code 0.
) else (
    echo [run_dev] exit code %RC% - paste the error above into a GitHub issue.
)

popd >nul 2>nul
exit /b %RC%
