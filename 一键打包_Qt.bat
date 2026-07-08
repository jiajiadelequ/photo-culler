@echo off
cd /d %~dp0
echo ============================================
echo   照片筛选 - PySide6 版本打包
echo ============================================
echo.
echo 使用虚拟环境: E:\python-envs\photo-culler-qt
echo.

set "VENV_PYTHON=E:\python-envs\photo-culler-qt\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo [错误] 未找到虚拟环境 Python，请先运行安装脚本。
    pause
    exit /b 1
)

echo [1/2] 清理旧构建文件...
if exist build_qt rmdir /s /q build_qt
if exist dist_release\照片筛选_Qt.exe del /q "dist_release\照片筛选_Qt.exe" 2>nul

echo [2/2] 开始打包...
"%VENV_PYTHON%" -m PyInstaller PhotoCullerQt.spec --distpath dist_release --workpath build_qt

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo   打包成功!
    echo   输出: dist_release\照片筛选.exe
    echo ============================================
) else (
    echo.
    echo [错误] 打包失败，请检查上方日志。
)

pause
