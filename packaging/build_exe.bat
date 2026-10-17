@echo off
rem Optional: builds a standalone OmniDL.exe for a PC without Python. You don't need this to
rem use OmniDL - start.bat (in the folder above) is how you run it.
rem Exits non-zero on any failure, and only says "Done" when OmniDL.exe really exists.
setlocal
cd /d "%~dp0"
echo ============================================
echo  Building OmniDL standalone app...
echo  (collect-all on spotdl/yt-dlp takes a few minutes)
echo ============================================

python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo.
    echo FAILED: couldn't install or update PyInstaller - nothing was built or changed.
    set "RC=1"
    goto :end
)

rem PyInstaller deletes the output folder before every build. The exe keeps its settings,
rem history and - with default settings - its downloads right next to itself, so they're set
rem aside first (same drive, so music is moved, not copied). If that fails, stop: building
rem would delete them.
set "OUT=%~dp0dist\OmniDL"
set "KEEP=%~dp0build\user-data-backup"
if not exist "%KEEP%" mkdir "%KEEP%"
if not exist "%KEEP%" (
    echo FAILED: couldn't create %KEEP% - nothing was built or changed.
    set "RC=1"
    goto :end
)
for %%F in (config.json history.json remote.json) do (
    if exist "%OUT%\%%F" (
        move /y "%OUT%\%%F" "%KEEP%\" >nul
        if exist "%OUT%\%%F" (
            echo FAILED: couldn't set aside %%F - nothing was built.
            set "RC=1"
            goto :restore
        )
    )
)
if exist "%OUT%\downloads" (
    move /y "%OUT%\downloads" "%KEEP%\downloads" >nul
    if exist "%OUT%\downloads" (
        echo FAILED: couldn't set aside the downloads folder - nothing was built.
        set "RC=1"
        goto :restore
    )
)

python -m PyInstaller OmniDL.spec --noconfirm --distpath "%~dp0dist" --workpath "%~dp0build"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo FAILED: the build stopped with an error (code %RC%).

:restore
rem Restore whatever was set aside, even after a failed build, so nothing is stranded.
if not exist "%OUT%" mkdir "%OUT%"
set "RESTORE_FAILED="
for %%F in (config.json history.json remote.json) do (
    if exist "%KEEP%\%%F" (
        move /y "%KEEP%\%%F" "%OUT%\" >nul
        if exist "%KEEP%\%%F" set "RESTORE_FAILED=1"
    )
)
if exist "%KEEP%\downloads" (
    move /y "%KEEP%\downloads" "%OUT%\downloads" >nul
    if exist "%KEEP%\downloads" set "RESTORE_FAILED=1"
)
if defined RESTORE_FAILED (
    echo WARNING: some of the app's settings/history/downloads are still in %KEEP% - move them
    echo back into %OUT% by hand.
    if "%RC%"=="0" set "RC=1"
)

if "%RC%"=="0" if not exist "%OUT%\OmniDL.exe" (
    echo FAILED: the build finished but %OUT%\OmniDL.exe isn't there.
    set "RC=1"
)

:end
echo.
if "%RC%"=="0" (
    echo Done. The app is in:  packaging\dist\OmniDL\OmniDL.exe
) else (
    echo The build did NOT complete. See the messages above.
)
if not defined OMNIDL_NO_PAUSE pause
exit /b %RC%
