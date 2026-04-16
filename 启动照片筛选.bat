@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto run_py

where python >nul 2>nul
if not errorlevel 1 goto run_python

echo Python 3 was not found.
echo Install Python and required packages first.
pause
exit /b 1

:run_py
py -3 photo_culler.py
if errorlevel 1 pause
exit /b %errorlevel%

:run_python
python photo_culler.py
if errorlevel 1 pause
exit /b %errorlevel%
