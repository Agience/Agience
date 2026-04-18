# ──────────────────────────────────────────────────────────────────────
# Agience — Home Install Script (Windows) — Edge Channel
#
# One shot: installs, starts Agience, and opens your browser.
# Uses edge images (latest main branch builds).
#
# Source: https://github.com/Agience/agience-core/blob/main/packaging/install/home/install-edge.ps1
#
# Usage:
#   irm https://get.agience.ai/home/install-edge.ps1 | iex
#
# After install:
#   agience up      start
#   agience down    stop
# ──────────────────────────────────────────────────────────────────────
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DataPath = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Configuration ────────────────────────────────────────────────────

$InstallDir = if ($DataPath) { $DataPath } else { Join-Path $env:USERPROFILE '.agience' }
$BinDir     = Join-Path $env:USERPROFILE '.agience\bin'
$ComposeUrl = 'https://raw.githubusercontent.com/Agience/agience-core/main/packaging/install/home/docker-compose.yml'

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
Write-Host "  |     Agience -- Install (edge)       |" -ForegroundColor Magenta
Write-Host "  |        home.agience.ai               |" -ForegroundColor Magenta
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
foreach ($port in @(80, 443)) {
    $used = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($used) { $conflicts += $port }
}

if ($conflicts.Count -gt 0) {
    Write-Warn "Ports in use: $($conflicts -join ', '). Stop those services before running 'agience up'."
} else {
    Write-Ok "Ports 80 and 443 are available"
}

# ── Step 3: Create Install Directory ────────────────────────────────

Write-Info "Install directory: $InstallDir"

$composeFile = Join-Path $InstallDir 'docker-compose.yml'
$isUpdate    = (Test-Path $composeFile)

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path $BinDir     -Force | Out-Null

if ($isUpdate) {
    Write-Warn "Existing installation found — updating and restarting"
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

# ── Step 4b: Set edge channel ───────────────────────────────────────

$envFile = Join-Path $InstallDir '.env'
if (Test-Path $envFile) {
    # Normalize: read raw bytes to detect missing trailing newline
    $raw = [System.IO.File]::ReadAllText($envFile)
    if ($raw.Length -gt 0 -and $raw[-1] -ne "`n") {
        $raw += "`n"
        [System.IO.File]::WriteAllText($envFile, $raw)
    }
    $lines = Get-Content $envFile | Where-Object { $_ -notmatch '^VERSION=' }
    $lines += 'VERSION=edge'
    Set-Content -Path $envFile -Value $lines -Encoding ASCII
} else {
    Set-Content -Path $envFile -Value 'VERSION=edge' -Encoding ASCII
}
Write-Ok "Channel set to edge"

# ── Step 5: Pull Images ─────────────────────────────────────────────

Write-Info "Pulling container images (this may take a few minutes)..."
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
echo Usage: agience [up^|down^|logs^|update^|status]
exit /b 1

:do_up
docker compose up -d
echo.
echo Agience is running. Open: https://home.agience.ai
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

Write-Ok "Agience is running"

# ── Read setup token ─────────────────────────────────────────────────

$tokenFile = Join-Path $InstallDir '.data\keys\setup.token'
$SetupToken = if (Test-Path $tokenFile) { (Get-Content $tokenFile -Raw).Trim() } else { '' }

# ── Step 8: Open browser ─────────────────────────────────────────────

Start-Process 'https://home.agience.ai'

# ── Done ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  +--------------------------------------+" -ForegroundColor Green
Write-Host "  |     Agience is running! (edge)      |" -ForegroundColor Green
Write-Host "  +--------------------------------------+" -ForegroundColor Green
Write-Host ""
Write-Host "  Open:   https://home.agience.ai" -ForegroundColor White
Write-Host "  Data:   $InstallDir\.data\" -ForegroundColor Gray
Write-Host ""
if ($SetupToken) {
    Write-Host "  Setup:  $SetupToken" -ForegroundColor Yellow
    Write-Host ""
}
Write-Host "  Commands:" -ForegroundColor White
Write-Host "    agience up        start"
Write-Host "    agience down      stop"
Write-Host "    agience logs      watch logs"
Write-Host "    agience update    pull latest images and restart"
Write-Host "    agience status    show running containers"
Write-Host ""

if ($pathUpdated) {
    Write-Host "  Note: Open a new terminal for 'agience' to be on your PATH." -ForegroundColor Yellow
    Write-Host ""
}
