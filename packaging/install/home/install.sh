#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Agience — Home Install Script (Linux / macOS)
#
# One shot: installs, starts Agience, and opens your browser.
#
# Source: https://github.com/Agience/agience-core/blob/main/packaging/install/home/install.sh
#
# Usage:
#   curl -fsSL https://get.agience.ai/home/install.sh | sh
#   bash install.sh
#
# After install:
#   agience up      # start
#   agience down    # stop
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────

INSTALL_DIR="${HOME}/.agience"
BIN_DIR="${HOME}/.local/bin"
COMPOSE_FILE="docker-compose.yml"
COMPOSE_URL="https://raw.githubusercontent.com/Agience/agience-core/main/packaging/install/home/docker-compose.yml"
DOCKER_INSTALL_URL="https://get.docker.com"
DOCKER_DESKTOP_MAC_URL="https://www.docker.com/products/docker-desktop/"

# Colors (disabled if not a terminal)
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' CYAN='' BOLD='' NC=''
fi

# ── Helpers ──────────────────────────────────────────────────────────

info()  { printf "${CYAN}[info]${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}[ok]${NC}    %s\n" "$*"; }
warn()  { printf "${YELLOW}[warn]${NC}  %s\n" "$*"; }
fail()  { printf "${RED}[error]${NC} %s\n" "$*" >&2; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

download() {
    local url=$1 dest=$2
    if command_exists curl; then
        curl -fsSL "$url" -o "$dest"
    elif command_exists wget; then
        wget -qO "$dest" "$url"
    else
        fail "Neither curl nor wget found. Cannot download files."
    fi
}

# ── Banner ───────────────────────────────────────────────────────────

printf "\n"
printf "${BOLD}  ╔══════════════════════════════════════╗${NC}\n"
printf "${BOLD}  ║         Agience — Install            ║${NC}\n"
printf "${BOLD}  ║         home.agience.ai              ║${NC}\n"
printf "${BOLD}  ╚══════════════════════════════════════╝${NC}\n"
printf "\n"

# ── Step 1: Detect OS ───────────────────────────────────────────────

OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Linux)  PLATFORM="linux" ;;
    Darwin) PLATFORM="macos" ;;
    *)      fail "Unsupported operating system: $OS. This script supports Linux and macOS." ;;
esac

info "Detected: $OS ($ARCH)"

# ── Step 2: Check / Install Docker ──────────────────────────────────

info "Checking for Docker..."

DOCKER_CMD="docker"
COMPOSE_CMD="docker compose"

install_docker_linux() {
    info "Docker not found. Installing Docker Engine (Community Edition)..."

    download "$DOCKER_INSTALL_URL" /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh

    if command_exists systemctl; then
        sudo systemctl start docker
        sudo systemctl enable docker
    fi

    if ! groups | grep -q docker; then
        sudo usermod -aG docker "$USER"
        warn "Added $USER to the docker group."
        warn "You may need to log out and back in for group changes to take effect."
        DOCKER_CMD="sudo docker"
        COMPOSE_CMD="sudo docker compose"
        return
    fi
}

check_docker() {
    if ! command_exists docker; then
        return 1
    fi

    if ! docker info >/dev/null 2>&1; then
        if [ "$PLATFORM" = "linux" ] && sudo docker info >/dev/null 2>&1; then
            DOCKER_CMD="sudo docker"
            COMPOSE_CMD="sudo docker compose"
            return 0
        fi
        return 1
    fi

    return 0
}

if check_docker; then
    ok "Docker is installed and running"
else
    case "$PLATFORM" in
        linux)
            install_docker_linux
            if ! $DOCKER_CMD info >/dev/null 2>&1; then
                fail "Docker installation completed but the daemon is not responding. Try: sudo systemctl start docker"
            fi
            ok "Docker installed successfully"
            ;;
        macos)
            fail "Docker is not installed or not running.

  Install Docker using one of these options:

  Option 1 — Docker Desktop (recommended):
    ${DOCKER_DESKTOP_MAC_URL}

  Option 2 — Colima (free, lightweight):
    brew install colima docker docker-compose
    colima start

  After installing, start Docker and run this script again."
            ;;
    esac
fi

if ! $COMPOSE_CMD version >/dev/null 2>&1; then
    fail "Docker Compose (V2 plugin) not found. Install it: https://docs.docker.com/compose/install/"
fi

ok "Docker Compose available"

# ── Step 3: Check Port Conflicts ────────────────────────────────────

info "Checking for port conflicts..."

check_port() {
    local port=$1
    if [ "$PLATFORM" = "linux" ]; then
        if command_exists ss; then
            ss -tlnp 2>/dev/null | grep -q ":${port} " && return 0
        elif command_exists netstat; then
            netstat -tlnp 2>/dev/null | grep -q ":${port} " && return 0
        fi
    elif [ "$PLATFORM" = "macos" ]; then
        if command_exists lsof; then
            lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
        fi
    fi
    return 1
}

PORT_CONFLICT=false
if check_port 80; then
    warn "Port 80 is already in use"
    PORT_CONFLICT=true
fi
if check_port 443; then
    warn "Port 443 is already in use"
    PORT_CONFLICT=true
fi

if $PORT_CONFLICT; then
    warn "Stop the service using these ports before starting Agience."
else
    ok "Ports 80 and 443 are available"
fi

# ── Step 4: Create Install Directory ────────────────────────────────

info "Install directory: $INSTALL_DIR"

if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/$COMPOSE_FILE" ]; then
    warn "Existing installation found — updating and restarting"
else
    mkdir -p "$INSTALL_DIR"
    ok "Created $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Step 5: Download Compose File ───────────────────────────────────

info "Downloading compose configuration..."

download "$COMPOSE_URL" "$COMPOSE_FILE"

if [ ! -s "$COMPOSE_FILE" ]; then
    fail "Downloaded compose file is empty. Check your network and try again."
fi

ok "Compose file downloaded"

# ── Step 6: Pull Images ─────────────────────────────────────────────

info "Pulling container images (this may take a few minutes)..."
printf "\n"

$COMPOSE_CMD pull

printf "\n"
ok "All images pulled"

# ── Step 7: Install agience CLI ─────────────────────────────────────

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
        echo "Agience is running. Open: https://home.agience.ai"
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
    *)
        echo "Usage: agience [up|down|logs|update|status]"
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

# ── Step 8: Start ────────────────────────────────────────────────────

info "Starting Agience..."
$COMPOSE_CMD up -d

ok "Agience is running"

# ── Read setup token ─────────────────────────────────────────────────

SETUP_TOKEN=""
TOKEN_FILE="${INSTALL_DIR}/.data/keys/setup.token"
if [ -f "$TOKEN_FILE" ]; then
    SETUP_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null || sudo cat "$TOKEN_FILE" 2>/dev/null || true)
fi

# ── Step 9: Open browser ─────────────────────────────────────────────

if command_exists xdg-open; then
    xdg-open https://home.agience.ai >/dev/null 2>&1 &
elif command_exists open; then
    open https://home.agience.ai
fi

# ── Done ─────────────────────────────────────────────────────────────

printf "\n"
printf "${BOLD}${GREEN}  ╔══════════════════════════════════════╗${NC}\n"
printf "${BOLD}${GREEN}  ║     Agience is running!              ║${NC}\n"
printf "${BOLD}${GREEN}  ╚══════════════════════════════════════╝${NC}\n"
printf "\n"
printf "  Open:   ${BOLD}https://home.agience.ai${NC}\n"
printf "  Data:   ${HOME}/.agience/.data/\n"
printf "\n"
if [ -n "$SETUP_TOKEN" ]; then
    printf "  ${YELLOW}Setup:  ${SETUP_TOKEN}${NC}\n"
    printf "\n"
fi
printf "  ${BOLD}Commands:${NC}\n"
printf "    agience up        start\n"
printf "    agience down      stop\n"
printf "    agience logs      watch logs\n"
printf "    agience update    pull latest images and restart\n"
printf "    agience status    show running containers\n"
printf "\n"

if $PATH_UPDATED; then
    printf "  ${YELLOW}Note:${NC} Open a new terminal (or run ${BOLD}source ~/.bashrc${NC}) for 'agience' to be on your PATH.\n"
    printf "\n"
fi
