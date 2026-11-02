@echo off
setlocal
cd /d "%~dp0"

set "RECREATE_ENV=0"
if not exist ".venv\Scripts\python.exe" set "RECREATE_ENV=1"
if exist ".venv\Scripts\python.exe" (
    if not exist ".venv\app-location.txt" set "RECREATE_ENV=1"
    if exist ".venv\app-location.txt" (
        findstr /x /l /c:"%CD%" ".venv\app-location.txt" >nul
        if errorlevel 1 set "RECREATE_ENV=1"
    )
)

if "%RECREATE_ENV%"=="1" (
    if exist ".venv" (
        echo Dang tao lai moi truong cho vi tri thu muc hien tai...
        rmdir /s /q ".venv"
    )
    echo Dang tao moi truong Python rieng...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Khong tim thay Python. Hay cai Python 3.10 tro len va chon Add Python to PATH.
            pause
            exit /b 1
        )
        python -m venv .venv
    )
    if errorlevel 1 (
        echo Tao moi truong Python that bai.
        pause
        exit /b 1
    )
    >".venv\app-location.txt" echo %CD%
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo Moi truong Python khong hop le. Hay xoa thu muc .venv va chay lai file nay.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\pip.exe" (
    echo Khong tim thay pip trong moi truong Python.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 (
    echo Ung dung can Python 3.10 tro len. Hay xoa thu muc .venv va cai Python moi.
    pause
    exit /b 1
)

echo Dang kiem tra/cai thu vien can thiet...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Cai thu vien that bai. Kiem tra ket noi Internet roi chay lai.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import tkinter, requests, dotenv" >nul 2>nul
if errorlevel 1 (
    echo Python thieu Tkinter hoac thu vien can thiet. Hay cai lai Python co Tcl/Tk.
    pause
    exit /b 1
)

if not exist "srt_tts_timeline.py" (
    echo Thieu file srt_tts_timeline.py trong thu muc ung dung.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 if not exist "ffmpeg\ffmpeg.exe" (
    echo Khong tim thay FFmpeg. Hay cai FFmpeg va them vao PATH, hoac dat ffmpeg.exe trong thu muc ffmpeg.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m py_compile app.py srt_tts_timeline.py
if errorlevel 1 (
    echo Khong the kiem tra ma nguon ung dung.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "app.py"
exit /b 0
