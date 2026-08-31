@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
title HSITX Ruckus Technical Tool - Build EXE
chcp 437 >nul

echo.
echo ============================================================
echo   Build HSITX_Ruckus_Technical_Tool.exe
echo ============================================================
echo Folder: %CD%
echo.

set "PY="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"
if not defined PY (
  for /f "delims=" %%I in ('where python 2^>nul') do (
    if not defined PY set "PY=%%I"
  )
)
if not defined PY (
  echo [ERROR] python.exe not found. Run run_script.bat first.
  goto END
)

echo Using: %PY%
"%PY%" --version
echo.

echo [*] Installing pyinstaller + libraries ...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install "pyinstaller==6.21.0" -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed.
  goto END
)

if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist HSITX_Ruckus_Technical_Tool.spec del /q HSITX_Ruckus_Technical_Tool.spec

echo [*] Building exe ...
"%PY%" -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name HSITX_Ruckus_Technical_Tool ^
  --add-data "modules;modules" ^
  --add-data "samples;samples" ^
  --hidden-import requests ^
  --hidden-import urllib3 ^
  --hidden-import paramiko ^
  --hidden-import cryptography ^
  --hidden-import bcrypt ^
  --hidden-import nacl ^
  --hidden-import openpyxl ^
  --hidden-import qrcode ^
  --hidden-import PIL ^
  --hidden-import tkinter ^
  gui_app.py

if errorlevel 1 (
  echo [ERROR] PyInstaller failed.
  goto END
)

echo.
echo [OK] Created:
echo     %CD%\dist\HSITX_Ruckus_Technical_Tool.exe
echo.
echo Copy that exe next to empty firmware / results / upload folders if needed.
echo First run may be slow. Windows Defender may scan it.
echo.

:END
echo Press any key to close...
pause
endlocal
