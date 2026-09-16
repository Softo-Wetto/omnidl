@echo off
cd /d "%~dp0"
echo ============================================
echo  Building OmniDL standalone app...
echo  (collect-all on spotdl/yt-dlp takes a few minutes)
echo ============================================
python -m pip install --upgrade pyinstaller

rem PyInstaller deletes dist\OmniDL before every build. The exe keeps its settings, history
rem and - with default settings - its downloads right next to itself, so without this a
rem rebuild silently wipes them. Set them aside (same drive, so music is moved, not copied).
set "KEEP=%~dp0build\user-data-backup"
if not exist "%KEEP%" mkdir "%KEEP%"
for %%F in (config.json history.json) do if exist "dist\OmniDL\%%F" move /y "dist\OmniDL\%%F" "%KEEP%\" >nul
if exist "dist\OmniDL\downloads" move /y "dist\OmniDL\downloads" "%KEEP%\downloads" >nul

python -m PyInstaller OmniDL.spec --noconfirm

rem Restore even if the build failed, so nothing is left stranded in the backup folder.
if not exist "dist\OmniDL" mkdir "dist\OmniDL"
for %%F in (config.json history.json) do if exist "%KEEP%\%%F" move /y "%KEEP%\%%F" "dist\OmniDL\" >nul
if exist "%KEEP%\downloads" move /y "%KEEP%\downloads" "dist\OmniDL\downloads" >nul

echo.
echo Done. Launch:  dist\OmniDL\OmniDL.exe
pause
