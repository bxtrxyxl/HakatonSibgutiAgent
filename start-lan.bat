@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem LAN mode: bind the server to all interfaces, then run the usual launcher.
set "HOST=0.0.0.0"
call "%~dp0start.bat"
exit /b %errorlevel%
