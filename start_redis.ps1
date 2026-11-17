# Starts native Windows Redis 5 on port 6380 in a minimized window and checks connection.
# Run this ONCE before starting Django.
#
# Usage: .\start_redis.ps1

$redisPath = Join-Path $PSScriptRoot "redis\redis-server.exe"
$cliPath = Join-Path $PSScriptRoot "redis\redis-cli.exe"

if (-not (Test-Path $redisPath)) {
    Write-Host "Redis binary not found at $redisPath" -ForegroundColor Red
    exit 1
}

$pong = & $cliPath -p 6380 ping 2>$null
if ($pong -ne "PONG") {
    Write-Host "Starting native Windows Redis 5 on port 6380..." -ForegroundColor Cyan
    Start-Process -FilePath $redisPath -ArgumentList "--port 6380" -WindowStyle Minimized
    Start-Sleep -Milliseconds 1200
    $pong = & $cliPath -p 6380 ping 2>$null
}

if ($pong -eq "PONG") {
    Write-Host "Native Windows Redis 5 is running and reachable at redis://localhost:6380" -ForegroundColor Green
} else {
    Write-Host "Redis did not respond. Check if port 6380 is blocked." -ForegroundColor Red
}
