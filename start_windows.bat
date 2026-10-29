@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 goto directory_error

rem Prefer an existing working virtual environment, then supported x64 Python.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import struct,sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
    if not errorlevel 1 goto use_venv
)
py -3.13 -c "import struct,sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto use_py313
py -3.12 -c "import struct,sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto use_py312
py -3.11 -c "import struct,sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto use_py311
python -c "import struct,sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto use_python

echo [ERROR] Install 64-bit Python 3.11, 3.12 or 3.13 first.
echo Official download: https://www.python.org/downloads/windows/
echo Select "Add Python to PATH", then reopen this launcher.
goto failed

:use_venv
".venv\Scripts\python.exe" "start_windows.py" %*
goto finished
:use_py313
py -3.13 "start_windows.py" %*
goto finished
:use_py312
py -3.12 "start_windows.py" %*
goto finished
:use_py311
py -3.11 "start_windows.py" %*
goto finished
:use_python
python "start_windows.py" %*
goto finished

:finished
set "LAUNCH_EXIT_CODE=%ERRORLEVEL%"
if not "%LAUNCH_EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Startup stopped. Read the message above before closing.
    pause
)
popd
exit /b %LAUNCH_EXIT_CODE%

:failed
pause
popd
exit /b 1

:directory_error
echo [ERROR] Cannot open the program directory. Extract the complete ZIP first.
pause
exit /b 1
