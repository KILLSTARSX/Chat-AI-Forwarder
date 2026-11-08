@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM Build the single-file Windows executable.
REM All paths are relative to this script - no machine-specific paths.

set "PY=python"
python -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 set "PY=py -3"

%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo PyInstaller not found, installing...
  %PY% -m pip install "pyinstaller>=6.0"
  if errorlevel 1 (
    echo Failed to install PyInstaller.
    exit /b 1
  )
)

echo Building single-file executable...
%PY% -m PyInstaller --noconfirm --clean ChatAIForwarder.spec
if errorlevel 1 (
  echo Build FAILED.
  exit /b 1
)

echo.
echo Done. Output is in the dist folder.
endlocal
