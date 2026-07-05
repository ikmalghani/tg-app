@echo off
setlocal EnableExtensions

set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"

set "PYENV_ROOT=%USERPROFILE%\.pyenv\pyenv-win"
set "PATH=%PYENV_ROOT%\bin;%PYENV_ROOT%\shims;%PATH%"

set "PYTHON="

if exist "%ROOT_DIR%\tg-upload\tgappenv\Scripts\python.exe" (
  set "PYTHON=%ROOT_DIR%\tg-upload\tgappenv\Scripts\python.exe"
) else if exist "%PYENV_ROOT%\versions\3.12.13\python.exe" (
  set "PYTHON=%PYENV_ROOT%\versions\3.12.13\python.exe"
)

if not defined PYTHON (
  echo Could not find pyenv Python (tgappenv or 3.12.13). >&2
  echo Install with: pyenv install 3.12.13, then create tg-upload\tgappenv. >&2
  exit /b 1
)

cd /d "%ROOT_DIR%"
"%PYTHON%" "%ROOT_DIR%\tg-app.py"
