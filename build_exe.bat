@echo off
setlocal
cd /d %~dp0
title Build Facebook ADB Tool - EXE

echo ========================================================
echo        DONG GOI FACEBOOK ADB TOOL THANH FILE .EXE
echo ========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo [1/3] Kiem tra cai dat PyInstaller...
%PYTHON_EXE% -m pip install pyinstaller --quiet

echo [2/3] Dang dong goi ban Portable EXE...
%PYTHON_EXE% -m PyInstaller --clean -y fb_tool_portable.spec

if %ERRORLEVEL% equ 0 (
    echo [3/3] Dong bo du lieu va cong cu...
    if exist "data" xcopy "data" "dist\fb_tool_portable\data" /E /I /Y /Q >nul 2>&1
    if exist "tools" xcopy "tools" "dist\fb_tool_portable\tools" /E /I /Y /Q >nul 2>&1
    if exist "assets" xcopy "assets" "dist\fb_tool_portable\assets" /E /I /Y /Q >nul 2>&1

    echo.
    echo ========================================================
    echo  [THANH CONG] DA DONG GOI XONG!
    echo.
    echo  DUONG DAN FILE CHAY:
    echo  dist\fb_tool_portable\fb_tool_portable.exe
    echo ========================================================
    echo.
    echo LUU Y: Ban chi can copy thu muc "dist\fb_tool_portable" sang
    echo may khac va chay file "fb_tool_portable.exe" (Khong mo thu muc "build").
) else (
    echo.
    echo [LOI] Qua trinh dong goi bi loi. Vui long kiem tra thong bao tren.
)

echo.
pause
endlocal
