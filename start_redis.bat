@echo off
set "REDIS_EXE=%~dp0redis\redis-server.exe"
set "REDIS_CLI=%~dp0redis\redis-cli.exe"

"%REDIS_CLI%" -p 6380 ping >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Starting Redis 5 on port 6380...
    start /min "Redis-Server-6380" "%REDIS_EXE%" --port 6380
    timeout /t 1 /nobreak >nul
)

"%REDIS_CLI%" -p 6380 ping
if %ERRORLEVEL% EQU 0 (
    echo Redis 5 is running and ready on port 6380!
) else (
    echo Failed to start Redis. Please check if port 6380 is in use.
)

