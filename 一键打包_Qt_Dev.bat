@echo off
cd /d "%~dp0"
echo ============================================
echo   Dev Build - PhotoCuller Qt (onedir)
echo ============================================
set "PY=E:\python-envs\photo-culler-qt\Scripts\python.exe"
set "TEMP=E:\Temp"
set "TMP=E:\Temp"
if not exist "%PY%" (echo [ERROR] venv not found && pause && exit /b 1)
if "%1"=="clean" (echo [*] Full rebuild... && if exist build_qt_dev rmdir /s /q build_qt_dev) else (echo [*] Incremental)
echo [*] Building to dist_dev ...
"%PY%" -m PyInstaller PhotoCullerQt_dev.spec --distpath dist_dev --workpath build_qt_dev --noconfirm --log-level WARN
if %errorlevel% equ 0 (echo   Done - dist_dev\照片筛选_Qt\) else (echo [ERROR] Build failed)
pause
