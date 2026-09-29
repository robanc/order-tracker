$ErrorActionPreference = 'Stop'
$responderRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $responderRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run uv sync --frozen first.' }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw 'Claude Code must be installed and authenticated.' }
$listener = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $health = Invoke-RestMethod 'http://127.0.0.1:8001/healthz'
    if ($health.mode -eq 'context-only') {
        Write-Output 'Responder already running at http://localhost:8001'
        exit 0
    }
    throw 'Port 8001 is already in use by another service.'
}
$runtimeDir = Join-Path $responderRoot 'runtime'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$process = Start-Process -FilePath $pythonExe -ArgumentList 'server.py' `
    -WorkingDirectory $responderRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $runtimeDir 'server.log') `
    -RedirectStandardError (Join-Path $runtimeDir 'server-error.log')
$process.Id | Set-Content (Join-Path $runtimeDir 'server.pid')
Write-Output "Responder started with PID $($process.Id) at http://localhost:8001"
