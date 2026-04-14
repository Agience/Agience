#!/usr/bin/env sh
# ──────────────────────────────────────────────────────────────────────
# Agience — Edge Install Script (Linux / macOS)
#
# One shot: pulls the latest edge images, starts Agience, opens your browser.
# No git clone, no build tools, no .env file required.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Agience/agience-core/main/packaging/install/edge/install.sh | sh
#
# After install:
#   agience up      start
#   agience down    stop
#   agience update  pull latest edge and restart
#   agience reset   wipe all data and start fresh
# ──────────────────────────────────────────────────────────────────────
set -e

# ── Configuration ────────────────────────────────────────────────────

INSTALL_DIR="${HOME}/.agience"
BIN_DIR="${HOME}/.local/bin"
COMPOSE_FILE="docker-compose.yml"
COMPOSE_URL="https://raw.githubusercontent.com/Agience/agience-core/main/packaging/install/edge/docker-compose.yml"

# ── Terminal colors ──────────────────────────────────────────────────

if [ -t 1 ]; then
    BOLD="\033[1m"; GREEN="\033[32m"; CYAN="\033[36m"
    YELLOW="\033[33m"; RED="\033[31m"; NC="\033[0m"
else
    BOLD=""; GREEN=""; CYAN=""; YELLOW=""; RED=""; NC=""
fi

info() { printf "${CYAN}  [info]   %s${NC}\n" "$*"; }
ok()   { printf "${GREEN}  [ok]     %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}  [warn]   %s${NC}\n" "$*"; }
fail() { printf "${RED}  [error]  %s${NC}\n" "$*"; exit 1; }

# ── Detect download tool ─────────────────────────────────────────────

command_exists() { command -v "$1" >/dev/null 2>&1; }

if command_exists curl; then
    download() { curl -fsSL "$1" -o "$2"; }
elif command_exists wget; then
    download() { wget -qO "$2" "$1"; }
else
    fail "curl or wget is required. Install one and try again."
fi

# ── Detect Docker Compose command ────────────────────────────────────

if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command_exists docker-compose; then
    COMPOSE_CMD="docker-compose"
else
    COMPOSE_CMD=""
fi

# ── Banner ───────────────────────────────────────────────────────────

printf "\n"
printf "${BOLD}  +--------------------------------------+${NC}\n"
printf "${BOLD}  |        Agience -- Edge Install       |${NC}\n"
printf "${BOLD}  |        latest main build             |${NC}\n"
printf "${BOLD}  +--------------------------------------+${NC}\n"
printf "\n"

# ── Step 1: Check Docker ─────────────────────────────────────────────

info "Checking for Docker..."

if ! command_exists docker; then
    fail "Docker is not installed or not on PATH.

  Install Docker Desktop:    https://www.docker.com/products/docker-desktop/
  Install Docker Engine:     https://docs.docker.com/engine/install/

  After installing, start Docker and run this script again."
fi

if ! docker info >/dev/null 2>&1; then
    fail "Docker is installed but not running. Start Docker and try again."
fi

if [ -z "$COMPOSE_CMD" ]; then
    fail "Docker Compose V2 not found. Update Docker Desktop / install the Compose plugin."
fi

ok "Docker is installed and running"

# ── Step 2: OpenSearch kernel tuning (Linux only) ────────────────────

if [ "$(uname -s)" = "Linux" ]; then
    info "Checking vm.max_map_count (required for OpenSearch)..."
    CURRENT_MAP_COUNT=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
    if [ "$CURRENT_MAP_COUNT" -lt 262144 ]; then
        warn "vm.max_map_count is $CURRENT_MAP_COUNT (need >= 262144)"
        if [ "$(id -u)" -eq 0 ]; then
            sysctl -w vm.max_map_count=262144
            ok "vm.max_map_count set to 262144"
        else
            warn "Run the following as root to fix, then retry:"
            warn "  sudo sysctl -w vm.max_map_count=262144"
            warn "To persist across reboots add to /etc/sysctl.conf:"
            warn "  vm.max_map_count=262144"
        fi
    else
        ok "vm.max_map_count is $CURRENT_MAP_COUNT"
    fi
fi

# ── Step 3: Create Install Directory ────────────────────────────────

info "Install directory: $INSTALL_DIR"

if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/$COMPOSE_FILE" ]; then
    warn "Existing installation found — updating compose file and restarting"
else
    mkdir -p "$INSTALL_DIR"
    ok "Created $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Step 4: Download Compose File ───────────────────────────────────

info "Downloading compose configuration..."

download "$COMPOSE_URL" "$COMPOSE_FILE"

if [ ! -s "$COMPOSE_FILE" ]; then
    fail "Downloaded compose file is empty. Check your network and try again."
fi

ok "Compose file downloaded"

# ── Step 5: Pull Images ─────────────────────────────────────────────

info "Pulling edge images (this may take a few minutes)..."
printf "\n"

$COMPOSE_CMD pull

printf "\n"
ok "All images pulled"

# ── Step 6: Install agience CLI ─────────────────────────────────────

info "Installing agience command..."

mkdir -p "$BIN_DIR"

cat > "${BIN_DIR}/agience" << AGIENCE_CLI
#!/usr/bin/env bash
AGIENCE_DIR="\${HOME}/.agience"
cd "\$AGIENCE_DIR" 2>/dev/null || { echo "Agience not found at \${AGIENCE_DIR}. Re-run the installer."; exit 1; }
case "\${1:-}" in
    up)
        docker compose up -d
        echo ""
        echo "Agience is running. Open: http://localhost:5173"
        ;;
    down)
        docker compose down
        ;;
    logs)
        docker compose logs -f
        ;;
    update)
        docker compose pull
        docker compose up -d
        ;;
    status)
        docker compose ps
        ;;
    reset)
        echo ""
        echo "============================================================"
        echo "  FACTORY RESET - THIS WILL PERMANENTLY DELETE ALL DATA"
        echo "============================================================"
        echo ""
        echo "  This will stop all containers and delete all persistent"
        echo "  data (database, object store, search index, keys)."
        echo "  The setup wizard will run on next start."
        echo ""
        printf "  Are you sure? [y/N] "
        read -r CONFIRM
        if [ "\${CONFIRM}" != "y" ] && [ "\${CONFIRM}" != "Y" ]; then
            echo "Aborted."
            exit 0
        fi
        echo ""
        echo "Stopping containers..."
        docker compose down
        DATA_DIR="\${AGIENCE_DIR}/.data"
        if [ -d "\${DATA_DIR}" ]; then
            echo "Deleting data..."
            rm -rf "\${DATA_DIR}"
            echo "Data deleted."
        else
            echo "No data directory found - already clean."
        fi
        echo ""
        echo "Reset complete. Run 'agience up' to start fresh."
        ;;
    *)
        echo "Usage: agience [up|down|logs|update|status|reset]"
        exit 1
        ;;
esac
AGIENCE_CLI

chmod +x "${BIN_DIR}/agience"
ok "agience CLI installed to ${BIN_DIR}/agience"

# Add ~/.local/bin to PATH if not already present
PATH_UPDATED=false
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    PATH_LINE="export PATH=\"\$PATH:${BIN_DIR}\""
    for rc in "${HOME}/.bashrc" "${HOME}/.zshrc" "${HOME}/.profile"; do
        if [ -f "$rc" ]; then
            if ! grep -qF "$BIN_DIR" "$rc" 2>/dev/null; then
                printf '\n# Added by Agience installer\n%s\n' "$PATH_LINE" >> "$rc"
            fi
        fi
    done
    export PATH="$PATH:${BIN_DIR}"
    PATH_UPDATED=true
fi

# ── Step 7: Start ────────────────────────────────────────────────────

info "Starting Agience..."
$COMPOSE_CMD up -d

ok "Agience is starting"

# ── Step 8: Wait for frontend and open browser ───────────────────────

info "Waiting for frontend to be ready..."

DEADLINE=$(($(date +%s) + 180))
OPENED=false
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    STATUS=$(curl -o /dev/null -s -w "%{http_code}" --max-time 3 http://127.0.0.1:5173 2>/dev/null || true)
    if [ "$STATUS" = "200" ]; then
        URL="http://localhost:5173"
        TOKEN_FILE="${INSTALL_DIR}/.data/keys/setup.token"
        if [ -f "$TOKEN_FILE" ]; then
            SETUP_NEEDS=$(curl -fsS --max-time 5 http://127.0.0.1:8081/setup/status 2>/dev/null | grep -o '"needs_setup":true' || true)
            if [ -n "$SETUP_NEEDS" ]; then
                TOKEN=$(cat "$TOKEN_FILE" | tr -d '[:space:]')
                URL="http://localhost:5173/setup?token=${TOKEN}"
            fi
        fi
        if command_exists xdg-open; then
            xdg-open "$URL" >/dev/null 2>&1 &
        elif command_exists open; then
            open "$URL"
        fi
        OPENED=true
        break
    fi
    sleep 3
done

if [ "$OPENED" = "false" ]; then
    warn "Frontend not ready yet — visit http://localhost:5173 once containers are healthy"
fi

# ── Done ─────────────────────────────────────────────────────────────

printf "\n"
printf "${BOLD}${GREEN}  ╔══════════════════════════════════════╗${NC}\n"
printf "${BOLD}${GREEN}  ║  Agience (edge) is running!        ║${NC}\n"
printf "${BOLD}${GREEN}  ╚══════════════════════════════════════╝${NC}\n"
printf "\n"
printf "  Open:      ${BOLD}http://localhost:5173${NC}\n"
printf "  API:       http://localhost:8081\n"
printf "  Servers:   http://localhost:8082\n"
printf "  Data:      ${HOME}/.agience/.data/\n"
printf "\n"
printf "  ${BOLD}Commands:${NC}\n"
printf "    agience up        start\n"
printf "    agience down      stop\n"
printf "    agience logs      watch logs\n"
printf "    agience update    pull latest edge images and restart\n"
printf "    agience status    show running containers\n"
    printf "    agience reset     wipe all data and start fresh\n"
if $PATH_UPDATED; then
    printf "  ${YELLOW}Note:${NC} Open a new terminal (or run ${BOLD}source ~/.bashrc${NC}) for 'agience' to be on your PATH.\n"
    printf "\n"
fi
