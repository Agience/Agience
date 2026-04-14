# ──────────────────────────────────────────────────────────────────────
# Agience — Edge Install Script (Windows)
#
# One shot: pulls the latest edge images, starts Agience, opens your browser.
# No git clone, no build tools, no .env file required.
#
# Usage:
#   irm https://raw.githubusercontent.com/Agience/agience-core/main/packaging/install/edge/install.ps1 | iex
#
# After install:
#   agience up      start
#   agience down    stop
#   agience update  pull latest edge and restart
# ──────────────────────────────────────────────────────────────────────
#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Configuration ────────────────────────────────────────────────────

$InstallDir = Join-Path $env:USERPROFILE '.agience'
$BinDir     = Join-Path $InstallDir 'bin'
$ComposeUrl = 'https://raw.githubusercontent.com/Agience/agience-core/main/packaging/install/edge/docker-compose.yml'

# ── Helpers ──────────────────────────────────────────────────────────

function Write-Info  { Write-Host "  [info]   $args" -ForegroundColor Cyan }
function Write-Ok    { Write-Host "  [ok]     $args" -ForegroundColor Green }
function Write-Warn  { Write-Host "  [warn]   $args" -ForegroundColor Yellow }
function Write-Fail  { Write-Host "  [error]  $args" -ForegroundColor Red; exit 1 }

function Test-Command { param($Name); return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Add-ToUserPath {
    param([string]$Dir)
    $current = [Environment]::GetEnvironmentVariable('PATH', 'User')
    if ($current -notlike "*$Dir*") {
        [Environment]::SetEnvironmentVariable('PATH', "$current;$Dir", 'User')
        $env:PATH = "$env:PATH;$Dir"
        return $true
    }
    return $false
}

# ── Banner ───────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  +--------------------------------------+" -ForegroundColor Magenta
Write-Host "  |        Agience -- Edge Install       |" -ForegroundColor Magenta
Write-Host "  |        latest main build             |" -ForegroundColor Magenta
Write-Host "  +--------------------------------------+" -ForegroundColor Magenta
Write-Host ""

# ── Step 1: Check Docker ─────────────────────────────────────────────

Write-Info "Checking for Docker..."

if (-not (Test-Command 'docker')) {
    Write-Fail @"
Docker Desktop is not installed or not on PATH.

  Install Docker Desktop for Windows:
  https://www.docker.com/products/docker-desktop/

  After installing, start Docker Desktop and run this script again.
"@
}

try {
    docker info 2>&1 | Out-Null
} catch {
    Write-Fail "Docker is installed but not running. Start Docker Desktop and try again."
}

if (-not (docker compose version 2>&1 | Select-String 'version')) {
    Write-Fail "Docker Compose V2 not found. Update Docker Desktop to a recent version."
}

Write-Ok "Docker is installed and running"

# ── Step 2: Check Port Conflicts ────────────────────────────────────

Write-Info "Checking for port conflicts..."

$conflicts = @()
foreach ($port in @(5173, 8081, 8082)) {
    $used = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($used) { $conflicts += $port }
}

if ($conflicts.Count -gt 0) {
    Write-Warn "Ports in use: $($conflicts -join ', '). Stop those services before running 'agience up'."
} else {
    Write-Ok "Ports 5173, 8081, and 8082 are available"
}

# ── Step 3: Create Install Directory ────────────────────────────────

Write-Info "Install directory: $InstallDir"

$composeFile = Join-Path $InstallDir 'docker-compose.yml'
$isUpdate    = (Test-Path $composeFile)

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path $BinDir     -Force | Out-Null

if ($isUpdate) {
    Write-Warn "Existing installation found — updating compose file and restarting"
} else {
    Write-Ok "Created $InstallDir"
}

# ── Step 4: Download Compose File ───────────────────────────────────

Write-Info "Downloading compose configuration..."

Invoke-WebRequest -Uri $ComposeUrl -OutFile $composeFile -UseBasicParsing

if ((Get-Item $composeFile).Length -eq 0) {
    Write-Fail "Downloaded compose file is empty. Check your network and try again."
}

Write-Ok "Compose file downloaded"

# ── Step 5: Pull Images ─────────────────────────────────────────────

Write-Info "Pulling edge images (this may take a few minutes)..."
Write-Host ""

Push-Location $InstallDir
docker compose pull
Pop-Location

Write-Host ""
Write-Ok "All images pulled"

# ── Step 6: Install agience.bat ─────────────────────────────────────

Write-Info "Installing agience command..."

$batPath = Join-Path $BinDir 'agience.bat'

$bat = @'
@echo off
setlocal
set AGIENCE_DIR=%USERPROFILE%\.agience
if not exist "%AGIENCE_DIR%\docker-compose.yml" (
    echo Agience not found at %AGIENCE_DIR%. Re-run the installer.
    exit /b 1
)
cd /d "%AGIENCE_DIR%"
if "%1"=="up"     goto do_up
if "%1"=="down"   goto do_down
if "%1"=="logs"   goto do_logs
if "%1"=="update" goto do_update
if "%1"=="status" goto do_status
if "%1"=="reset"  goto do_reset
echo Usage: agience [up^|down^|logs^|update^|status^|reset]
exit /b 1

:do_up
docker compose up -d
echo.
echo Agience is running. Open: http://localhost:5173
goto done

:do_down
docker compose down
goto done

:do_logs
docker compose logs -f
goto done

:do_update
docker compose pull
docker compose up -d
goto done

:do_status
docker compose ps
goto done

:do_reset
echo.
echo ============================================================
echo   FACTORY RESET - THIS WILL PERMANENTLY DELETE ALL DATA
echo ============================================================
echo.
echo   This will stop all containers and delete all persistent
echo   data (database, object store, search index, keys).
echo   The setup wizard will run on next start.
echo.
set /p "CONFIRM=   Are you sure? [y/N] "
if /i not "%CONFIRM%"=="y" (
    echo Aborted.
    goto done
)
echo.
echo Stopping containers...
docker compose down
echo Deleting data...
if exist "%AGIENCE_DIR%\.data" (
    rmdir /s /q "%AGIENCE_DIR%\.data"
    echo Data deleted.
) else (
    echo No data directory found - already clean.
)
echo.
echo Reset complete. Run 'agience up' to start fresh.
goto done

:done
endlocal
'@

Set-Content -Path $batPath -Value $bat -Encoding ASCII

Write-Ok "agience.bat installed to $BinDir"

$pathUpdated = Add-ToUserPath $BinDir

# ── Step 7: Start ────────────────────────────────────────────────────

Write-Info "Starting Agience..."

Push-Location $InstallDir
docker compose up -d
Pop-Location

Write-Ok "Agience is starting"

# ── Step 8: Wait for frontend and open browser ───────────────────────

Write-Info "Waiting for frontend to be ready..."

$deadline = (Get-Date).AddSeconds(180)
$opened = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173 -TimeoutSec 3 -ErrorAction Stop
        if ($r.StatusCode -eq 200) {
            $url = 'http://localhost:5173'
            $tokenFile = Join-Path $InstallDir '.data\keys\setup.token'
            if (Test-Path $tokenFile) {
                try {
                    $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8081/setup/status' -TimeoutSec 5 -ErrorAction Stop
                    if ($status.needs_setup) {
                        $token = (Get-Content $tokenFile -Raw).Trim()
                        $url = "http://localhost:5173/setup?token=$token"
                    }
                } catch {}
            }
            Start-Process $url
            $opened = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 3
}

if (-not $opened) {
    Write-Warn "Frontend not ready yet — visit http://localhost:5173 once containers are healthy"
}

# ── Done ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  +--------------------------------------+" -ForegroundColor Green
Write-Host "  |     Agience (edge) is running!     |" -ForegroundColor Green
Write-Host "  +--------------------------------------+" -ForegroundColor Green
Write-Host ""
Write-Host "  Open:     http://localhost:5173" -ForegroundColor White
Write-Host "  API:      http://localhost:8081" -ForegroundColor Gray
Write-Host "  Servers:  http://localhost:8082" -ForegroundColor Gray
Write-Host "  Data:     $InstallDir\.data\" -ForegroundColor Gray
Write-Host ""
Write-Host "  Commands:" -ForegroundColor White
Write-Host "    agience up        start"
Write-Host "    agience down      stop"
Write-Host "    agience logs      watch logs"
Write-Host "    agience update    pull latest edge images and restart"
Write-Host "    agience status    show running containers"
Write-Host "    agience reset     wipe all data and start fresh"
Write-Host ""

if ($pathUpdated) {
    Write-Host "  Note: Open a new terminal for 'agience' to be on your PATH." -ForegroundColor Yellow
    Write-Host ""
}
