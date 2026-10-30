@echo off
setlocal
title WAIFU - Stop
echo Stopping WaifuStudio...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_app.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_llm.ps1" -Port 8291
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_llm.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_engine.ps1"
pause
endlocal
