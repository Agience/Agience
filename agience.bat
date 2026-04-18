@echo off
setlocal enabledelayedexpansion

REM Always resolve relative paths from this script's directory.
set "SCRIPT_DIR=%~dp0"
cd /d "%~dp0"

REM Parse command line arguments
set MODE=full
set FORCE_DOCKER=false
set BUILD_DOCKER=false
set INSTALL_DEPS=false
set CLEAN_DEPS=false
set DO_RESET=false
set CONTAINERS_JUST_STARTED=false
REM REGISTRY is only relevant for deploy mode (remote pull). Local builds tag
REM images as agience/* but never pull from Docker Hub or GHCR.
if not defined REGISTRY set REGISTRY=agience

REM Venv location for backend dependencies (dev mode)
set "VENV_DIR=%SCRIPT_DIR%backend\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

REM Service groups
set INFRA=content graph search
set SERVERS=servers stream

if "%1"=="--help" goto help
if "%1"=="-h" goto help
if "%1"=="help" goto help
if "%1"=="down" goto cmd_down
if "%1"=="stop" goto cmd_down

REM Parse all arguments
:parse_loop
if "%1"=="" goto parse_done
if "%1"=="full" set MODE=full
if "%1"=="dev" set MODE=dev
if "%1"=="canary" set MODE=canary
if "%1"=="test" set MODE=test
if "%1"=="--force-docker" set FORCE_DOCKER=true
if "%1"=="-f" set FORCE_DOCKER=true
if "%1"=="--build" set BUILD_DOCKER=true
if "%1"=="-b" set BUILD_DOCKER=true
if "%1"=="--registry" (
    set REGISTRY=%2
    shift
)
if "%1"=="-r" (
    set REGISTRY=%2
    shift
)
if "%1"=="--install-deps" set INSTALL_DEPS=true
if "%1"=="-i" set INSTALL_DEPS=true
if "%1"=="--clean-deps" set CLEAN_DEPS=true
if "%1"=="--reset" set DO_RESET=true
shift
goto parse_loop
:parse_done

if "%DO_RESET%"=="true" goto do_reset
goto start_setup

:do_reset
echo.
echo ============================================================
echo   FACTORY RESET - THIS WILL PERMANENTLY DELETE ALL DATA
echo ============================================================
echo.
echo   This will:
echo     1. Stop all Agience containers
echo     2. Delete all persistent data (database, object store,
echo        search index, and RSA keys)
echo     3. Start fresh (mode: %MODE%)
echo.
echo   After reset, the setup wizard will run on next launch.
echo.

REM Resolve DATA_PATH from .env (if present) or fall back to ./.data
set "DATA_PATH_RESOLVED=.\..data"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        setlocal enabledelayedexpansion
        set "_K=%%A"
        set "_K=!_K: =!"
        endlocal & if /i "%%A"=="DATA_PATH" set "DATA_PATH_RESOLVED=%%B"
    )
)
REM Default fallback if still unset
if "%DATA_PATH_RESOLVED%"==".\..data" set "DATA_PATH_RESOLVED=.\.data"

echo   Data path: %DATA_PATH_RESOLVED%
echo.
set /p "CONFIRM=   Are you sure? [y/N] "
if /i not "%CONFIRM%"=="y" (
    echo Aborted.
    exit /b 0
)

echo.
echo Stopping all containers...
docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.servers.yml down
if errorlevel 1 (
    echo WARNING: Some containers may not have stopped cleanly. Continuing...
)

echo Stopping local backend/frontend processes...
powershell -NoProfile -Command "$targets = @(8081, 5173); foreach ($port in $targets) { $pids = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($listenerPid in $pids) { $proc = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listenerPid) -ErrorAction SilentlyContinue; if (-not $proc) { continue }; if ($port -eq 8081 -and $proc.CommandLine -match '\bmain\.py\b') { Start-Process taskkill -ArgumentList '/PID', $listenerPid, '/T', '/F' -NoNewWindow -Wait; Write-Host 'Backend stopped.' }; if ($port -eq 5173 -and $proc.CommandLine -match 'vite|npm run dev|node.*vite') { Start-Process taskkill -ArgumentList '/PID', $listenerPid, '/T', '/F' -NoNewWindow -Wait; Write-Host 'Frontend stopped.' } } }" 2>nul

echo.
echo Deleting data at: %DATA_PATH_RESOLVED%
if exist "%DATA_PATH_RESOLVED%" (
    rmdir /s /q "%DATA_PATH_RESOLVED%" 2>nul
    if exist "%DATA_PATH_RESOLVED%" (
        powershell -NoProfile -Command "Remove-Item -LiteralPath '%DATA_PATH_RESOLVED%' -Recurse -Force -ErrorAction SilentlyContinue"
    )
    if exist "%DATA_PATH_RESOLVED%" (
        echo ERROR: Could not fully remove %DATA_PATH_RESOLVED%. Close any applications using that folder and retry.
        exit /b 1
    )
    echo Data deleted [OK]
) else (
    echo Data directory not found - already clean [OK]
)
echo.
goto start_setup

:cmd_down
echo Stopping all Agience containers...
docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.servers.yml down
exit /b %errorlevel%

:help
echo ====================================
echo    Agience
echo ====================================
echo.
echo Usage: agience [mode] [options]
echo.
echo Modes:
echo   full    - Everything in Docker: infra + backend + frontend + servers (default)
echo   dev     - Infra + servers in Docker, backend + frontend run locally
echo             (restarts local backend/frontend by default)
echo   canary  - Pull and run pre-built :canary images from Docker Hub (no build required)
echo   test    - Run precheck: backend lint + tests, frontend lint + tests
echo.
echo Options:
echo   --force-docker, -f   - Force restart Docker containers (stop then start)
echo   --build, -b          - Rebuild Docker images before starting (picks up code changes)
echo   --registry, -r NAME  - Docker Hub namespace for images (default: agience)
echo   --install-deps, -i   - Force install/refresh dependencies (npm install, pip install)
echo   --clean-deps         - Clean install dependencies (npm ci, pip uninstall + install)
echo   --reset              - Factory reset: wipe all data before starting
echo   --help, -h, help     - Show this help message
echo.
echo Examples:
echo   agience                      # Full stack in Docker (default)
echo   agience canary               # Pull and run canary (latest main build from Docker Hub)
echo   agience canary -f            # Force restart canary containers
echo   agience dev                  # Dev mode: infra in Docker, backend+frontend local
echo   agience dev -f --build       # Dev mode, force restart and rebuild
echo   agience dev -i               # Dev mode, reinstall dependencies
echo   agience down                 # Stop all containers
echo   agience test                 # Run lint + tests
echo   agience --reset              # Factory reset, restart full
echo   agience dev -f --build --reset  # Factory reset, restart dev
echo.
pause
exit /b 0

:start_setup

REM Test mode: run precheck and exit (no Docker needed)
if "%MODE%"=="test" (
    echo ====================================
    echo    Agience - Precheck
    echo ====================================
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%.scripts\precheck.ps1"
    exit /b %errorlevel%
)

echo ====================================
echo    Agience
echo    Mode: %MODE%
echo    Registry: %REGISTRY%
echo ====================================
echo.

REM Check if Docker is running (required for both modes)
echo [1/4] Checking Docker status...
docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is not running. Please start Docker Engine and try again.
    pause
    exit /b 1
)
echo Docker is running [OK]

REM Check if Node.js is installed (not needed in full/canary mode - frontend runs in Docker)
if "%MODE%"=="full" (
    echo [2/4] Skipping Node.js check - frontend runs in Docker
) else (
    if "%MODE%"=="canary" (
        echo [2/4] Skipping Node.js check - frontend runs in Docker
    ) else (
        echo [2/4] Checking Node.js...
        node --version >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Node.js is not installed. Please install Node.js and try again.
            pause
            exit /b 1
        )
        echo Node.js is available [OK]
    )
)

REM Check if Python is installed (not needed in full/canary mode - backend runs in Docker)
if "%MODE%"=="full" (
    echo [3/4] Skipping Python check - backend runs in Docker
) else (
    if "%MODE%"=="canary" (
        echo [3/4] Skipping Python check - backend runs in Docker
    ) else (
        echo [3/4] Checking Python...
        python --version >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Python is not installed. Please install Python and try again.
            pause
            exit /b 1
        )
        echo Python is available [OK]
    )
)

REM Handle Docker services based on mode
if "%MODE%"=="full" goto docker_full
if "%MODE%"=="canary" goto docker_canary
goto docker_dev

:docker_full
echo [4/4] Managing Docker services (full mode - all services)...
set "COMPOSE_UP_FLAGS=-d"
if "%BUILD_DOCKER%"=="true" set "COMPOSE_UP_FLAGS=--build -d"
if "%FORCE_DOCKER%"=="true" (
    echo Restarting all Docker containers...
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.override.yml -f docker/docker-compose.servers.yml down
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.override.yml -f docker/docker-compose.servers.yml up !COMPOSE_UP_FLAGS!
    if errorlevel 1 ( echo ERROR: Failed to start Docker services. & pause & exit /b 1 )
    echo Docker services restarted [OK]
    set CONTAINERS_JUST_STARTED=true
    goto docker_done
)
echo Checking if containers are running...
docker compose --project-directory . -f docker/docker-compose.yml ps graph 2>nul | findstr /i "graph" >nul 2>&1
if errorlevel 1 (
    echo Starting Docker services...
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.override.yml -f docker/docker-compose.servers.yml up !COMPOSE_UP_FLAGS!
    if errorlevel 1 ( echo ERROR: Failed to start Docker services. & pause & exit /b 1 )
    echo Docker services started [OK]
    set CONTAINERS_JUST_STARTED=true
) else (
    echo Docker services already running [OK]
    echo Use --force-docker to rebuild and restart
    set CONTAINERS_JUST_STARTED=false
)
goto docker_done

:docker_canary
echo [4/4] Managing Docker services (canary mode - pulling registry images)...
set "VERSION=canary"
set "SERVERS_IMAGE=agience/agience-servers:canary"
set "STREAM_IMAGE=agience/agience-stream:canary"
if "%FORCE_DOCKER%"=="true" (
    echo Restarting all canary containers...
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.servers.yml down
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.servers.yml up --pull=always -d
    if errorlevel 1 ( echo ERROR: Failed to start canary services. & pause & exit /b 1 )
    echo Canary containers restarted [OK]
    set CONTAINERS_JUST_STARTED=true
    goto docker_done
)
echo Checking if containers are running...
docker compose --project-directory . -f docker/docker-compose.yml ps graph 2>nul | findstr /i "graph" >nul 2>&1
if errorlevel 1 (
    echo Pulling and starting canary services...
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.servers.yml up --pull=always -d
    if errorlevel 1 ( echo ERROR: Failed to start canary services. & pause & exit /b 1 )
    echo Canary services started [OK]
    set CONTAINERS_JUST_STARTED=true
) else (
    echo Canary containers already running [OK]
    echo Use --force-docker to pull latest and restart
    set CONTAINERS_JUST_STARTED=false
)
goto docker_done

:docker_dev
echo [4/4] Managing Docker services (dev mode - infra + servers)...
set "COMPOSE_UP_FLAGS=-d"
if "%BUILD_DOCKER%"=="true" set "COMPOSE_UP_FLAGS=--build -d"
if "%FORCE_DOCKER%"=="true" (
    echo Restarting server containers - infra left running...
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.override.yml -f docker/docker-compose.servers.yml rm -sf !SERVERS!
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.override.yml -f docker/docker-compose.servers.yml up !COMPOSE_UP_FLAGS! !INFRA! !SERVERS!
    if errorlevel 1 ( echo ERROR: Failed to start Docker services. & pause & exit /b 1 )
    echo Server containers restarted [OK]
    set CONTAINERS_JUST_STARTED=true
    goto docker_done
)
echo Checking if infra containers are running...
docker compose --project-directory . -f docker/docker-compose.yml ps graph 2>nul | findstr /i "graph" >nul 2>&1
if errorlevel 1 (
    echo Starting infra and server containers...
    docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.override.yml -f docker/docker-compose.servers.yml up !COMPOSE_UP_FLAGS! !INFRA! !SERVERS!
    if errorlevel 1 ( echo ERROR: Failed to start Docker services. & pause & exit /b 1 )
    echo Infra and server containers started [OK]
    set CONTAINERS_JUST_STARTED=true
) else (
    echo Infra and server containers already running [OK]
    echo Use --force-docker to restart
    set CONTAINERS_JUST_STARTED=false
)

echo Ensuring Docker app containers are stopped for local dev...
docker compose --project-directory . -f docker/docker-compose.yml rm -sf backend frontend >nul 2>&1

:docker_done

REM Auto-check and install dependencies in dev mode
if "%MODE%"=="dev" (
    if "%INSTALL_DEPS%"=="false" if "%CLEAN_DEPS%"=="false" (
        echo.
        echo Checking dependencies...
        
        REM Check if frontend node_modules exists
        set NEED_FRONTEND_DEPS=false
        if not exist "frontend\node_modules" (
            echo Frontend dependencies missing
            set NEED_FRONTEND_DEPS=true
        )
        
        REM Ensure backend venv exists
        set NEED_BACKEND_DEPS=false
        if not exist "!VENV_DIR!" (
            echo Creating backend virtual environment...
            python -m venv "!VENV_DIR!"
            set NEED_BACKEND_DEPS=true
        ) else (
            REM Check if backend dependencies are installed (check for mcp package)
            "!VENV_PYTHON!" -c "import mcp" >nul 2>&1
            if errorlevel 1 (
                echo Backend dependencies missing
                set NEED_BACKEND_DEPS=true
            ) else (
                REM Check if requirements.txt changed since last install
                set "REQ_STAMP=!VENV_DIR!\.requirements_stamp"
                if not exist "!REQ_STAMP!" (
                    echo Backend dependencies stamp missing - will reinstall
                    set NEED_BACKEND_DEPS=true
                ) else (
                    fc /b "%SCRIPT_DIR%backend\requirements.txt" "!REQ_STAMP!" >nul 2>&1
                    if errorlevel 1 (
                        echo Backend requirements.txt changed - will reinstall
                        set NEED_BACKEND_DEPS=true
                    )
                )
            )
        )
        
        REM Install if needed
        if "!NEED_FRONTEND_DEPS!"=="true" (
            call :install_frontend_deps
            set "NPM_EXIT=!ERRORLEVEL!"
            if not "!NPM_EXIT!"=="0" (
                echo ERROR: Failed to install frontend dependencies.
                pause
                exit /b 1
            )
            echo Frontend dependencies installed [OK]
        ) else (
            echo Frontend dependencies OK
        )
        
        if "!NEED_BACKEND_DEPS!"=="true" (
            echo Installing backend dependencies into .venv...
            pushd "%SCRIPT_DIR%backend"
            "!VENV_PIP!" install -r requirements.txt
            set "PIP_EXIT=!ERRORLEVEL!"
            popd
            if not "!PIP_EXIT!"=="0" (
                echo ERROR: Failed to install backend dependencies.
                pause
                exit /b 1
            )
            copy /y "%SCRIPT_DIR%backend\requirements.txt" "!VENV_DIR!\.requirements_stamp" >nul
            echo Backend dependencies installed [OK]
        ) else (
            echo Backend dependencies OK
        )
    )
)

REM Install dependencies if requested
if "%INSTALL_DEPS%"=="true" (
    call :install_frontend_deps
    set "NPM_EXIT=!ERRORLEVEL!"
    if not "!NPM_EXIT!"=="0" (
        echo ERROR: Failed to install frontend dependencies.
        pause
        exit /b 1
    )

    echo Installing backend dependencies into .venv...
    if not exist "!VENV_DIR!" python -m venv "!VENV_DIR!"
    pushd "%SCRIPT_DIR%backend"
    "!VENV_PIP!" install -r requirements.txt
    set "PIP_EXIT=!ERRORLEVEL!"
    popd
    if not "!PIP_EXIT!"=="0" (
        echo ERROR: Failed to install backend dependencies.
        pause
        exit /b 1
    )
    copy /y "%SCRIPT_DIR%backend\requirements.txt" "!VENV_DIR!\.requirements_stamp" >nul
    echo Dependencies installed [OK]
)

REM Clean install dependencies if requested
if "%CLEAN_DEPS%"=="true" (
    call :clean_frontend_deps
    set "NPM_EXIT=!ERRORLEVEL!"
    if not "!NPM_EXIT!"=="0" (
        echo ERROR: Failed to clean install frontend dependencies.
        pause
        exit /b 1
    )

    echo Clean installing backend dependencies...
    if exist "!VENV_DIR!" (
        echo Removing existing .venv...
        rmdir /s /q "!VENV_DIR!" 2>nul
        if exist "!VENV_DIR!" powershell -NoProfile -Command "Remove-Item -LiteralPath '!VENV_DIR!' -Recurse -Force -ErrorAction SilentlyContinue"
    )
    echo Creating fresh virtual environment...
    python -m venv "!VENV_DIR!"
    pushd "%SCRIPT_DIR%backend"
    echo Installing fresh backend dependencies...
    "!VENV_PIP!" install -r requirements.txt
    set "PIP_EXIT=!ERRORLEVEL!"
    popd
    if not "!PIP_EXIT!"=="0" (
        echo ERROR: Failed to clean install backend dependencies.
        pause
        exit /b 1
    )
    copy /y "%SCRIPT_DIR%backend\requirements.txt" "!VENV_DIR!\.requirements_stamp" >nul
    echo Dependencies clean installed [OK]
)

goto deps_done

:install_frontend_deps
echo Installing frontend dependencies...
pushd "%SCRIPT_DIR%frontend"
if exist package-lock.json (
    call npm ci
) else (
    echo No package-lock.json found, using npm install...
    call npm install
)
set "NPM_EXIT=!ERRORLEVEL!"
popd
exit /b !NPM_EXIT!

:clean_frontend_deps
echo Clean installing frontend dependencies...
pushd "%SCRIPT_DIR%frontend"
if exist node_modules (
    echo Removing existing node_modules...
    rmdir /s /q node_modules
    if exist node_modules powershell -NoProfile -Command "Remove-Item -LiteralPath 'node_modules' -Recurse -Force -ErrorAction SilentlyContinue"
)
if exist package-lock.json (
    call npm ci
) else (
    echo No package-lock.json found, using npm install...
    call npm install
)
set "NPM_EXIT=!ERRORLEVEL!"
popd
exit /b !NPM_EXIT!

:deps_done

if "%MODE%"=="full" (
    echo.
    echo ====================================
    echo   All services running in Docker!
    echo ====================================
    echo.
    echo   Backend:  http://localhost:8081
    echo   Frontend: http://localhost:5173
    echo.
    echo   MCP Servers:  http://localhost:8082
    echo     /aria/mcp  /sage/mcp    /atlas/mcp  /nexus/mcp
    echo     /astra/mcp /verso/mcp   /seraph/mcp /ophan/mcp
    echo     Stream:      rtmp://localhost:1936/live
    echo.
    echo   To stop: agience down
    echo.
    if "%CONTAINERS_JUST_STARTED%"=="true" (
        echo   Opening browser once the frontend is ready...
        powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(120); $opened = $false; while ((Get-Date) -lt $deadline) { try { $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173 -TimeoutSec 3 -ErrorAction Stop; if ($r.StatusCode -eq 200) { $url = 'http://localhost:5173'; $tokenFile = '.\.data\keys\setup.token'; $needsSetup = $false; $token = ''; if (Test-Path $tokenFile) { try { $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8081/setup/status' -TimeoutSec 5 -ErrorAction Stop; if ($status.needs_setup) { $needsSetup = $true; $token = (Get-Content $tokenFile -Raw).Trim(); $url = \"http://localhost:5173/setup?token=$token\" } } catch {} }; Start-Process $url; if ($needsSetup) { Write-Host ''; Write-Host ('=' * 64) -ForegroundColor Yellow; Write-Host ''; Write-Host '   SETUP REQUIRED - open this URL to get started:' -ForegroundColor Yellow; Write-Host ''; Write-Host \"   $url\" -ForegroundColor Cyan; Write-Host ''; Write-Host \"   Token: $token\" -ForegroundColor Cyan; Write-Host ''; Write-Host ('=' * 64) -ForegroundColor Yellow; Write-Host '' }; $opened = $true; break } } catch {} Start-Sleep -Seconds 2 }; if (-not $opened) { Write-Host '  Browser not opened - visit http://localhost:5173 manually' }"
    )
    exit /b 0
)

if "%MODE%"=="canary" (
    echo.
    echo ====================================
    echo   Canary build running in Docker!
    echo ====================================
    echo.
    echo   Backend:  http://localhost:8081
    echo   Frontend: http://localhost:5173
    echo.
    echo   MCP Servers:  http://localhost:8082
    echo     /aria/mcp  /sage/mcp    /atlas/mcp  /nexus/mcp
    echo     /astra/mcp /verso/mcp   /seraph/mcp /ophan/mcp
    echo     Stream:      rtmp://localhost:1936/live
    echo.
    echo   To stop: agience down
    echo.
    if "%CONTAINERS_JUST_STARTED%"=="true" (
        echo   Opening browser once the frontend is ready...
        powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(120); $opened = $false; while ((Get-Date) -lt $deadline) { try { $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173 -TimeoutSec 3 -ErrorAction Stop; if ($r.StatusCode -eq 200) { $url = 'http://localhost:5173'; $tokenFile = '.\..data\keys\setup.token'; $needsSetup = $false; $token = ''; if (Test-Path $tokenFile) { try { $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8081/setup/status' -TimeoutSec 5 -ErrorAction Stop; if ($status.needs_setup) { $needsSetup = $true; $token = (Get-Content $tokenFile -Raw).Trim(); $url = \"http://localhost:5173/setup?token=$token\" } } catch {} }; Start-Process $url; if ($needsSetup) { Write-Host ''; Write-Host ('=' * 64) -ForegroundColor Yellow; Write-Host ''; Write-Host '   SETUP REQUIRED - open this URL to get started:' -ForegroundColor Yellow; Write-Host ''; Write-Host \"   $url\" -ForegroundColor Cyan; Write-Host ''; Write-Host \"   Token: $token\" -ForegroundColor Cyan; Write-Host ''; Write-Host ('=' * 64) -ForegroundColor Yellow; Write-Host '' }; $opened = $true; break } } catch {} Start-Sleep -Seconds 2 }; if (-not $opened) { Write-Host '  Browser not opened - visit http://localhost:5173 manually' }"
    )
    exit /b 0
)

echo.
echo ====================================
echo   Starting Development Servers
echo ====================================
echo.
echo   Backend:  http://localhost:8081
echo   Frontend: http://localhost:5173
echo.
echo   MCP Servers (Docker): http://localhost:8082
    echo     /aria /sage /atlas /nexus /astra /verso /seraph /ophan
    echo     Stream: rtmp://localhost:1936/live
echo.

if "%MODE%"=="dev" (
    echo Restarting local backend/frontend listeners...
    powershell -NoProfile -Command "$targets = @(8081, 5173); foreach ($port in $targets) { $pids = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($listenerPid in $pids) { $proc = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listenerPid) -ErrorAction SilentlyContinue; if (-not $proc) { continue }; if ($port -eq 8081 -and $proc.CommandLine -match '\bmain\.py\b') { Start-Process taskkill -ArgumentList '/PID', $listenerPid, '/T', '/F' -NoNewWindow -Wait }; if ($port -eq 5173 -and $proc.CommandLine -match 'vite|npm run dev|node.*vite') { Start-Process taskkill -ArgumentList '/PID', $listenerPid, '/T', '/F' -NoNewWindow -Wait } } }" >nul 2>&1
    timeout /t 1 /nobreak >nul
)

REM Check if backend process is actually LISTENING on port 8081 (not just dangling connections)
netstat -ano 2>nul | findstr /c:":8081 " | findstr /i "LISTENING" >nul 2>&1
set BACKEND_RUNNING=%errorlevel%
set BACKEND_HEALTHY=false

REM If something is listening on 8081, only skip launch if the Agience backend is
REM actually responding. Stale local python main.py listeners can otherwise block
REM dev startup indefinitely.
if "%BACKEND_RUNNING%"=="0" (
    powershell -NoProfile -Command "try { $response = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8081/version -TimeoutSec 3; if ($response.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
    if errorlevel 1 (
        echo Existing listener on port 8081 is unhealthy. Removing stale local backend processes...
        powershell -NoProfile -Command "$pids = Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($listenerPid in $pids) { $proc = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listenerPid) -ErrorAction SilentlyContinue; if ($proc -and $proc.CommandLine -match '\bmain\.py\b') { Start-Process taskkill -ArgumentList '/PID', $listenerPid, '/T', '/F' -NoNewWindow -Wait } }" >nul 2>&1
        timeout /t 1 /nobreak >nul
        netstat -ano 2>nul | findstr /c:":8081 " | findstr /i "LISTENING" >nul 2>&1
        set BACKEND_RUNNING=%errorlevel%
        if "%BACKEND_RUNNING%"=="0" (
            echo Port 8081 is still occupied. Stop the conflicting process, then rerun agience dev.
        )
    ) else (
        set BACKEND_HEALTHY=true
    )
)

REM Check if frontend process is actually LISTENING on port 5173 (not just dangling connections)
netstat -ano 2>nul | findstr /c:":5173 " | findstr /i "LISTENING" >nul 2>&1
set FRONTEND_RUNNING=%errorlevel%

REM Track if we need to launch anything
set NEED_TO_LAUNCH=false

REM Build the Windows Terminal command with both tabs in one window
set WT_CMD=wt.exe

REM Add frontend tab (only if not already running)
if %FRONTEND_RUNNING% neq 0 (
    echo Starting frontend server...
    set WT_CMD=!WT_CMD! --title "Frontend" -p "Command Prompt" -d "%cd%\frontend" cmd /k "npm run dev"
    set NEED_TO_LAUNCH=true
) else (
    echo Frontend already running on port 5173 - skipping
)

REM Add backend tab (only if not already running)
if %BACKEND_RUNNING% neq 0 (
    echo Starting backend server...
    set WT_CMD=!WT_CMD! ; nt --title "Backend" -p "Command Prompt" -d "%cd%\backend" cmd /k ""%cd%\backend\.venv\Scripts\python.exe" main.py"
    set NEED_TO_LAUNCH=true
) else (
    if /i "!BACKEND_HEALTHY!"=="true" (
        echo Backend already healthy on port 8081 - skipping
    ) else (
        echo Backend already running on port 8081 - skipping
    )
)

echo.

REM Only launch terminal if something needs to start
if "%NEED_TO_LAUNCH%"=="true" (
    echo Launching in new Windows Terminal window with separate tabs...
    start !WT_CMD!
) else (
    echo All servers already running - no new terminal opened.
    exit /b 0
)

echo.
echo Servers are launching in Windows Terminal tabs!
echo.
REM Show setup token/link if generated by init container
if exist ".data\keys\setup.token" (
    for /f "usebackq delims=" %%T in (".data\keys\setup.token") do set SETUP_TOKEN=%%T
    echo ====================================
    echo   FIRST-TIME SETUP
    echo ====================================
    echo   Open this link in your browser:
    echo.
    echo   http://localhost:5173/setup?token=!SETUP_TOKEN!
    echo.
    echo   Token: !SETUP_TOKEN!
    echo ====================================
    echo.
)
echo   Visit: http://localhost:5173
echo.
echo To stop servers: Close each tab or press Ctrl+C
echo To stop Docker:  agience down
echo.

goto end

:end
