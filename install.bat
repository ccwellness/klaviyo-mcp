@echo off
REM ===========================================================================
REM install.bat - one-step setup for klaviyo-mcp on Windows.
REM
REM Ensures a Python 3.11 virtual environment with the dependencies installed,
REM then runs the configurator (install.py). Any arguments are passed straight
REM through to install.py.
REM
REM   install.bat                 scaffold config + validate + print MCP config
REM   install.bat --check-api     also verify each key against Klaviyo
REM   install.bat --no-scaffold   validate only (do not write templates)
REM ===========================================================================

cd /d "%~dp0"
set "VENV_PY=.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto run

echo No .venv found - creating one on Python 3.11...

REM 1) Windows Python launcher (py -3.11)
py -3.11 -m venv .venv 1>nul 2>nul
if exist "%VENV_PY%" goto deps

REM 2) uv (if installed)
where uv 1>nul 2>nul && uv venv --python 3.11 .venv 1>nul 2>nul
if exist "%VENV_PY%" goto deps

echo.
echo ERROR: could not create a Python 3.11 virtual environment automatically.
echo Install Python 3.11 ^(or the 'uv' tool^), then create it manually and re-run:
echo     py -3.11 -m venv .venv
exit /b 1

:deps
echo Installing dependencies ^(hash-verified^)...
"%VENV_PY%" -m pip install --upgrade pip 1>nul
"%VENV_PY%" -m pip install --require-hashes -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    exit /b 1
)

:run
"%VENV_PY%" install.py %*
exit /b %errorlevel%
