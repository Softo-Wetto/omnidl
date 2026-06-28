@echo off
cd /d "%~dp0"
echo ============================================
echo  Building OmniDL standalone app...
echo  (collect-all on spotdl/yt-dlp takes a few minutes)
echo ============================================
python -m pip install --upgrade pyinstaller
python -m PyInstaller OmniDL.spec --noconfirm
echo.
echo Done. Launch:  dist\OmniDL\OmniDL.exe
pause
