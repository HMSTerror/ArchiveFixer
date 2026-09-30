@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher not found. Install Python 3.13 from https://www.python.org/downloads/windows/
    exit /b 1
)

py -3.13 -m venv .venv
if errorlevel 1 (
    echo Python 3.13 is required to build this release.
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt "pyinstaller>=6.16,<7"
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" build_release.py
if errorlevel 1 exit /b 1

echo Built folder: %~dp0dist\ArchiveFixer
echo Built package: %~dp0dist\ArchiveFixer-portable.zip
echo Publish ArchiveFixer-portable.zip and THIRD_PARTY_NOTICES.txt in GitHub Releases.
exit /b 0
