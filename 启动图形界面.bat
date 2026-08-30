@echo off
setlocal
chcp 65001 >nul
title FormatConverter Launcher

set "SCRIPT_DIR=%~dp0"
set "PY="
set "PYARGS="

REM --- Python detection (1: venv, 2: py -3, 3: python) ---
REM Test hook: FC_TEST_PYTHON overrides detection (used by smoke tests).
if defined FC_TEST_PYTHON (
    set "PY=%FC_TEST_PYTHON%"
    set "PYARGS="
    goto :python_found
)

REM 1) Prefer the in-repo virtualenv
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    set "PY=%VENV_PY%"
    goto :python_found
)

REM 2) py -3
set "PY=py"
set "PYARGS=-3"
where py >nul 2>nul
if not errorlevel 1 goto :python_found

REM 3) python
set "PY=python"
set "PYARGS="
where python >nul 2>nul
if not errorlevel 1 goto :python_found

goto :no_python

:no_python
echo.
echo [ERROR] Python was not found.
echo Please install Python 3.11 or newer:  https://www.python.org/downloads/
echo Then, from the project root, run these commands to set up the environment:
echo.
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo     .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
echo.
if not defined FC_TEST_NO_PAUSE pause
exit /b 1

:python_found
echo Using Python: %PY% %PYARGS%

REM --- Core dependency check ---
"%PY%" %PYARGS% -c "import format_converter.web_server" >nul 2>nul
if errorlevel 1 goto :deps_missing

REM --- Optional dependency checks (non-fatal, continue anyway) ---
"%PY%" %PYARGS% -c "import openai" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [NOTE] openai is missing: the AI proofreading card will not work.
    echo        To install:  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
"%PY%" %PYARGS% -c "import pymupdf4llm" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [NOTE] pymupdf4llm is missing: PDF conversion will not work.
    echo        To install:  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo.
echo Starting FormatConverter local service (bound to 127.0.0.1 only)...
echo This window is the service window; keep it open. Press Ctrl+C to stop.
echo.

"%PY%" %PYARGS% -m format_converter.web_server
set "EXIT_CODE=%errorlevel%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] The service failed to start or exited abnormally (code %EXIT_CODE%).
    echo See the messages above for details.
    if not defined FC_TEST_NO_PAUSE pause
    exit /b %EXIT_CODE%
)

exit /b 0

:deps_missing
echo.
echo [ERROR] Python 3 or the project dependencies are not ready.
echo Please install Python 3.11 or newer, then from the project root run:
echo.
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo     .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
echo.
echo Notes:
echo   - 'openai'       is required for AI proofreading (ai-clean)
echo   - 'pymupdf4llm'  is required for PDF conversion (convert/pipeline)
echo   - clean and the local UI do not need those optional packages.
echo.
if not defined FC_TEST_NO_PAUSE pause
exit /b 1
