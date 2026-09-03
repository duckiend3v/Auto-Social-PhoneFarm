@echo off
chcp 65001 >nul
echo ===================================================
echo   DỌN DẸP TIẾN TRÌNH VÀ KHÔI PHỤC ADB CHUẨN
echo ===================================================
echo.

echo [1/3] Đang tắt các tiến trình adb và _cache_adb bị treo...
taskkill /f /im _cache_adb.exe >nul 2>&1
taskkill /f /im adb.exe >nul 2>&1

echo [2/3] Đang sao chép bản ADB chuẩn từ TLCHelper...
if not exist "tools" mkdir "tools"
if not exist "tools\platform-tools" mkdir "tools\platform-tools"

if exist "C:\TLCHelper\sdk\platform-tools\adb.exe" (
    copy /y "C:\TLCHelper\sdk\platform-tools\adb.exe" "tools\adb.exe" >nul
    copy /y "C:\TLCHelper\sdk\platform-tools\AdbWinApi.dll" "tools\AdbWinApi.dll" >nul 2>&1
    copy /y "C:\TLCHelper\sdk\platform-tools\AdbWinUsbApi.dll" "tools\AdbWinUsbApi.dll" >nul 2>&1

    copy /y "C:\TLCHelper\sdk\platform-tools\adb.exe" "tools\platform-tools\adb.exe" >nul
    copy /y "C:\TLCHelper\sdk\platform-tools\AdbWinApi.dll" "tools\platform-tools\AdbWinApi.dll" >nul 2>&1
    copy /y "C:\TLCHelper\sdk\platform-tools\AdbWinUsbApi.dll" "tools\platform-tools\AdbWinUsbApi.dll" >nul 2>&1
    echo -> Đã sao chép ADB thành công từ C:\TLCHelper\sdk\platform-tools!
) else (
    echo -> Không tìm thấy C:\TLCHelper\sdk\platform-tools\adb.exe, giữ nguyên file ADB hiện tại.
)

echo.
echo [3/3] Kiểm tra danh sách thiết bị nhận diện:
if exist "tools\adb.exe" (
    "tools\adb.exe" devices
) else if exist "tools\platform-tools\adb.exe" (
    "tools\platform-tools\adb.exe" devices
)

echo.
echo ===================================================
echo HOÀN TẤT! Bạn có thể mở lại phần mềm và Quét thiết bị.
echo ===================================================
pause
