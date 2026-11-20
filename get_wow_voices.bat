@echo off
rem Download WoW NPC voices into voice_library\wow\ and transcribe new clips with Whisper.
rem
rem   get_wow_voices.bat          only the WoW voices PST is cast with (pst_cast\casting.json + pst-cast-overrides.json)
rem   get_wow_voices.bat all      every WoW voice group, so pst_cast.py has the full pool to pick from
rem
rem Needs a RetailSounds checkout for the community listfile (set RETAILSOUNDS_DATA or answer the prompt),
rem ffmpeg on PATH and internet (files come from wago.tools). Clips already on disk are skipped, and finished
rem reference.wav files are kept, so compiled Drama 3 voices don't change.
title PST - WoW voices
setlocal
cd /d "%~dp0"
set PYTHON=python
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYTHON="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
where ffmpeg >nul 2>nul || (echo ffmpeg is not on PATH. Install it ^(e.g. C:\ffmpeg\bin^) and try again. & goto fail)

set MODE=--cast-only
if /i "%~1"=="all" set MODE=

if not defined RETAILSOUNDS_DATA set "RETAILSOUNDS_DATA=E:\GitHub\RetailSounds\data"
if exist "%RETAILSOUNDS_DATA%\community-listfile.csv" goto have_listfile
echo No community-listfile.csv in "%RETAILSOUNDS_DATA%".
set /p "RETAILSOUNDS_DATA=RetailSounds data folder: "
if not exist "%RETAILSOUNDS_DATA%\community-listfile.csv" (echo Still no community-listfile.csv there. & goto fail)
:have_listfile

%PYTHON% voicelib_wow.py %MODE% --retailsounds "%RETAILSOUNDS_DATA%" || goto fail

rem Whisper (torch + transformers) lives in its own venv: local .venv-omnivoice, else the VVE_Builder one
if not defined WHISPER_PYTHON if exist ".venv-omnivoice\Scripts\python.exe" set "WHISPER_PYTHON=%CD%\.venv-omnivoice\Scripts\python.exe"
if not defined WHISPER_PYTHON if exist "D:\VVE_Builder-main\.venv-omnivoice\Scripts\python.exe" set "WHISPER_PYTHON=D:\VVE_Builder-main\.venv-omnivoice\Scripts\python.exe"
if not defined WHISPER_PYTHON (echo Whisper venv not found: skipped transcripts. Set WHISPER_PYTHON and run voicelib_transcribe.py. & goto done)
"%WHISPER_PYTHON%" voicelib_transcribe.py || goto fail

:done
echo.
echo WoW voices are in voice_library\wow\
pause
exit /b 0

:fail
echo.
echo Failed.
pause
exit /b 1
