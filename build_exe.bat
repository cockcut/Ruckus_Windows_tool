@echo off
setlocal EnableDelayedExpansion
title HSITX Ruckus Technical Tool - Build EXE
chcp 437 >nul
cd /d "%~dp0"

if not exist "gui_app.py" (
  echo [ERROR] gui_app.py not found. Run this bat in the Tool folder.
  goto END
)

set "EXENAME=HSITX_Ruckus_Technical_Tool"

echo.
echo ============================================================
echo   Build %EXENAME%.exe
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
  echo [ERROR] python.exe not found.
  goto END
)

echo Using: %PY%
"%PY%" --version
echo.

echo [*] Installing pyinstaller 6.21.0 + Tool requirements ...
"%PY%" -m pip install --upgrade pip
if exist requirements.txt (
  "%PY%" -m pip install "pyinstaller==6.21.0" -r requirements.txt
) else (
  "%PY%" -m pip install "pyinstaller==6.21.0" paramiko requests urllib3 openpyxl qrcode pillow
)
if errorlevel 1 (
  echo [ERROR] pip install failed.
  goto END
)

if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist "%EXENAME%.spec" del /q "%EXENAME%.spec"

echo [*] Building ...
"%PY%" -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name "%EXENAME%" ^
  --add-data "modules;modules" ^
  --add-data "samples;samples" ^
  --hidden-import requests --hidden-import urllib3 --hidden-import tkinter ^
  --hidden-import paramiko --hidden-import openpyxl --hidden-import qrcode --hidden-import PIL ^
  gui_app.py

if errorlevel 1 (
  echo [ERROR] PyInstaller failed.
  goto END
)

echo.
echo [OK] %CD%\dist\%EXENAME%.exe
echo Copy the exe next to this folder or keep dist\ as the release.

:END
echo.
echo Press any key to close...
pause
endlocal
