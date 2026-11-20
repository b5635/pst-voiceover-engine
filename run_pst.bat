@echo off
rem PST Revoice — Fish Audio Drama 3.
rem One window for setup and daily work. The GUI says what is missing and what to click next.
title PST Revoice
cd /d "%~dp0"

if not defined SPEECH_EXE set SPEECH_EXE=D:\fish-drama3\speech\speech.exe
if not defined DRAMA3_MODEL set DRAMA3_MODEL=C:\fish-drama3\models\drama-3-final\Drama-35B-A3B-v3-Q4_K.gguf
if not defined DRAMA3_DEVICE set DRAMA3_DEVICE=4060 Ti

if not defined WHISPER_PYTHON if not exist ".venv-omnivoice\Scripts\python.exe" if exist "D:\VVE_Builder-main\.venv-omnivoice\Scripts\python.exe" set "WHISPER_PYTHON=D:\VVE_Builder-main\.venv-omnivoice\Scripts\python.exe"

set PYTHON=python
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYTHON="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

%PYTHON% pst_gui.py %*
if errorlevel 1 pause
