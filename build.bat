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

".venv\Scripts\python.exe" -c "from pathlib import Path; import shutil; root = Path.cwd().resolve(); target = (root / 'build').resolve(); assert target.parent == root; shutil.rmtree(target, ignore_errors=True)"
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --onefile --windowed --name ArchiveFixer --collect-all tkinterdnd2 --add-data "THIRD_PARTY_NOTICES.txt;." main.py
if errorlevel 1 exit /b 1

echo Built: %~dp0dist\ArchiveFixer.exe
echo Publish the EXE together with THIRD_PARTY_NOTICES.txt in GitHub Releases.
exit /b 0
