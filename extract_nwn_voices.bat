@echo off
rem Extract the NWN: Enhanced Edition soundsets into voice_library\nwn\ (24 kHz WAV + transcript + reference per voice).
rem
rem   extract_nwn_voices.bat                                   asks for the install folder (remembers it)
rem   extract_nwn_voices.bat "D:\Steam\steamapps\common\Neverwinter Nights"
rem
rem Point it at the folder that holds data\nwn_base.key and lang\en\data\dialog.tlk. Needs ffmpeg on PATH.
title PST - NWN voices
setlocal
cd /d "%~dp0"
set PYTHON=python
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYTHON="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
where ffmpeg >nul 2>nul || (echo ffmpeg is not on PATH. Install it ^(e.g. C:\ffmpeg\bin^) and try again. & goto fail)

set "NWN_DIR=%~1"
if "%NWN_DIR%"=="" if exist nwn-path.txt set /p NWN_DIR=<nwn-path.txt
if "%NWN_DIR%"=="" set "NWN_DIR=E:\SteamLibrary\steamapps\common\Neverwinter Nights"

:check
if exist "%NWN_DIR%\data\nwn_base.key" if exist "%NWN_DIR%\lang\en\data\dialog.tlk" goto have_nwn
echo No NWN: Enhanced Edition install at "%NWN_DIR%" (need data\nwn_base.key and lang\en\data\dialog.tlk).
set "NWN_DIR="
set /p "NWN_DIR=Neverwinter Nights EE folder (blank to quit): "
if "%NWN_DIR%"=="" goto fail
set "NWN_DIR=%NWN_DIR:"=%"
:trim
if "%NWN_DIR:~-1%"==" " (set "NWN_DIR=%NWN_DIR:~0,-1%" & goto trim)
goto check
:have_nwn
> nwn-path.txt echo %NWN_DIR%

echo Extracting from "%NWN_DIR%"
%PYTHON% voicelib_nwn.py --nwn "%NWN_DIR%" || goto fail
echo.
echo NWN voices are in voice_library\nwn\
pause
exit /b 0

:fail
echo.
echo Failed.
pause
exit /b 1
