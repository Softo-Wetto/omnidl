@echo off
rem Optional: builds a standalone OmniDL.exe for a PC without Python. You don't need this to
rem use OmniDL - start.bat (in the folder above) is how you run it.
cd /d "%~dp0"
echo ============================================
echo  Building OmniDL standalone app...
echo  (collect-all on spotdl/yt-dlp takes a few minutes)
echo ============================================
python -m pip install --upgrade pyinstaller

rem PyInstaller deletes the output folder before every build. The exe keeps its settings,
rem history and - with default settings - its downloads right next to itself, so without this
rem a rebuild silently wipes them. Set them aside (same drive, so music is moved, not copied).
set "OUT=%~dp0dist\OmniDL"
set "KEEP=%~dp0build\user-data-backup"
if not exist "%KEEP%" mkdir "%KEEP%"
for %%F in (config.json history.json remote.json) do if exist "%OUT%\%%F" move /y "%OUT%\%%F" "%KEEP%\" >nul
if exist "%OUT%\downloads" move /y "%OUT%\downloads" "%KEEP%\downloads" >nul

python -m PyInstaller OmniDL.spec --noconfirm --distpath "%~dp0dist" --workpath "%~dp0build"

rem Restore even if the build failed, so nothing is left stranded in the backup folder.
if not exist "%OUT%" mkdir "%OUT%"
for %%F in (config.json history.json remote.json) do if exist "%KEEP%\%%F" move /y "%KEEP%\%%F" "%OUT%\" >nul
if exist "%KEEP%\downloads" move /y "%KEEP%\downloads" "%OUT%\downloads" >nul

echo.
echo Done. The app is in:  packaging\dist\OmniDL\OmniDL.exe
pause
