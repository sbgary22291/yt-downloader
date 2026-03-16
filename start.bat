@echo off
chcp 65001 >nul
echo.
echo ============================================
echo   YT Downloader 啟動中...
echo ============================================
echo.

cd /d "%~dp0"
set PATH=%PATH%;C:\Users\sbgar\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python，請先安裝 Python 3
    pause
    exit /b
)

REM Install dependencies if needed
if not exist "venv" (
    echo [1/2] 建立虛擬環境...
    python -m venv venv
    echo [2/2] 安裝套件...
    call venv\Scripts\activate.bat
    pip install -r requirements.txt --quiet
) else (
    call venv\Scripts\activate.bat
)

REM Check ffmpeg
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo [提醒] 未偵測到 ffmpeg，高畫質影片可能無法合併。
    echo         請執行: winget install ffmpeg
    echo.
)

echo.
echo  Windows 防火牆可能會跳出詢問，請點「允許」
echo  這樣 iPad 才能透過 Wi-Fi 連線使用
echo.

python app.py
pause
