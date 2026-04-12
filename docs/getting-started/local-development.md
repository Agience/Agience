# Local Development Setup

Status: **Reference**
Date: 2026-04-10

This guide walks a developer through running Agience locally from source: infrastructure containers, backend, frontend, and (optionally) the desktop relay host. It is not a production or self-hosting guide — see `docs/getting-started/self-hosting.md` for that.

> **Just want to run Agience?** Use the [home install](https://github.com/Agience/agience-home) — one command, pre-built images, no build tools needed.

---

## Quick Start

The fastest path for developers:

```bash
git clone https://github.com/Agience/agience-core.git
cd agience-core
```

**Windows:**
```
agience dev -f --build
```

**Linux / macOS:**
```bash
./agience dev -f --build
```

This will:
1. Check that Docker, Python, and Node.js are installed
2. Start infrastructure containers (ArangoDB, OpenSearch, MinIO) + MCP servers in Docker
3. Create a Python virtual environment at `backend/.venv` and install dependencies
4. Install frontend npm dependencies
5. Launch the backend and frontend in a new Windows Terminal window

On **first boot**, the init container generates all credentials (encryption keys, database passwords, platform secret, setup token). Open `http://localhost:5173` — the setup wizard walks you through OAuth login and LLM provider configuration. No `.env` file is required.

---

## 1. Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| **Git** | any recent | version control |
| **Docker Desktop** | latest stable | includes Docker Compose v2 |
| **Python** | 3.11+ | backend runtime |
| **Node.js** | 20+ | frontend build/dev server |
| **npm** | bundled with Node | frontend package management |

**Windows note**: Docker Desktop for Windows requires either WSL 2 or Hyper-V. WSL 2 is recommended. Verify Docker is running before proceeding:

```powershell
docker info
```

**Python note**: The `agience` launcher automatically creates and uses a virtual environment at `backend/.venv`. If you run the backend manually, activate it first:

```powershell
backend\.venv\Scripts\Activate.ps1    # Windows PowerShell
# source backend/.venv/bin/activate    # bash / Git Bash
```

---

## 2. Clone and Configure

### Clone the repository

```bash
git clone https://github.com/Agience/agience-core.git
cd agience-core
```

### Configuration

**Zero-config (recommended):** The init container generates all credentials on first boot. The setup wizard (browser) configures OAuth login, LLM provider, and access control. No `.env` file needed.

**Manual override (optional):** If you prefer to set environment variables directly, copy the template and edit:

```powershell
Copy-Item .env.example .env.local
```

The backend reads `.env.local` first (local/test), then `.env` (Docker defaults), then falls back to database settings from the setup wizard. For most developers, the setup wizard is sufficient.

---

## 3. Start Infrastructure (Dev Mode)

The `agience dev` command handles everything. For manual control:

Dev mode starts the database services and MCP servers in Docker, leaving the backend and frontend running as local processes for faster iteration.

```bash
docker compose --project-directory . \
  -f docker/docker-compose.yml \
  -f docker/docker-compose.override.yml \
  up -d content graph search
```

On Windows PowerShell (no line continuation needed, or use backtick):

```powershell
docker compose --project-directory . `
  -f docker/docker-compose.yml `
  -f docker/docker-compose.override.yml `
  up -d content graph search
```

Wait for all three containers to reach the `healthy` state:

```bash
docker ps
```

Expected output (abbreviated):

```
CONTAINER ID   IMAGE         STATUS
...            arangodb      Up ... (healthy)
...            opensearch    Up ... (healthy)
```

**What each service is:**

| Service name | Technology | Purpose | Local port |
|---|---|---|---|
| `graph` | ArangoDB | All artifact storage (workspaces + collections) | 8529 |
| `search` | OpenSearch | Hybrid BM25 + kNN search | 9200 |

These ports are intentionally non-standard to avoid collisions with other local services.

**Stopping the infrastructure:**

```bash
docker compose --project-directory . \
  -f docker/docker-compose.yml \
  -f docker/docker-compose.override.yml \
  down
```

---

## 4. Run the Backend

In a new terminal, with the venv active:

```bash
cd backend
.venv/Scripts/activate        # Windows — or: source .venv/bin/activate (bash)
pip install -r requirements.txt
python main.py
```

> **Tip**: If you used `agience dev`, the venv at `backend/.venv` is already created and dependencies are installed. Just activate it.

The backend starts on `http://localhost:8081`.

**First-run behavior**: On the first startup the backend:

1. Creates ArangoDB collections and indexes
2. Provisions the OpenSearch application user and indexes
3. Seeds authority collections, inbox sources, and default LLM connection artifacts for new users

You will see log output for each of these steps. If a database container is not yet healthy, the backend retries automatically for a short window. If startup fails, check `docker ps` to confirm both containers are running and healthy.

**Log level**: Set `BACKEND_LOG_LEVEL=DEBUG` in `.env.local` for verbose output during development. The default is `DEBUG` in the template.

**Interactive API docs**: Once the backend is running, FastAPI's Swagger UI is available at:

```
http://localhost:8081/docs
```

---

## 5. Run the Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

The dev server starts at `http://localhost:5173` and proxies API calls to the backend at `http://localhost:8081`.

**What `npm run dev` does:**

- Vite reads `build_info.json` and injects `__APP_VERSION__` as a compile-time define
- Starts the Vite HMR (Hot Module Replacement) dev server
- Opens `http://localhost:5173` — navigate there in your browser

**First login**: Click "Sign in with Google", authenticate, and you will be redirected back to the app. If you see "Access denied", your email or domain is not in the access control settings (configured via the setup wizard, or `ALLOWED_EMAILS` / `ALLOWED_DOMAINS` in `.env.local`).

After first login, the backend provisions your user's inbox, workspace, and seed collections automatically.

---

## 6. Desktop Relay Host (Optional)

The desktop relay host is an optional local companion runtime that:

- Exposes the first-party MCP persona servers locally (Aria, Sage, Atlas, etc.)
- Provides safe read-only filesystem tools via MCP
- Can maintain a relay connection to a running backend authority

This is only needed if you are working on the desktop host itself, testing local MCP persona servers, or developing features that use the relay protocol.

### Install

From the repo root:

```powershell
Set-Location hosts\desktop
python -m pip install -e .
```

### Configure

Copy `hosts/desktop/config.example.json` to a local config file and edit:

```json
{
  "mode": "host",
  "authority_url": "http://localhost:8081",
  "access_token": "<agience access token from local sign-in>",
  "bind_host": "127.0.0.1",
  "bind_port": 8082,
  "allowed_roots": [
    "C:/Users/yourname/Documents"
  ],
  "enabled_personas": ["aria", "sage", "atlas", "nexus", "astra", "verso", "seraph", "ophan"],
  "log_level": "INFO"
}
```

### Start

```powershell
python -m agience_relay_host.main --config .\config.example.json
```

For full setup instructions, token generation steps, and troubleshooting, see `hosts/desktop/README.md`.

---

## 7. Verify

Work through this checklist after starting the full stack.

**Backend health**

```bash
curl http://localhost:8081/version
```

Expected: a JSON object with `version` and build metadata.

```bash
curl http://localhost:8081/.well-known/mcp.json
```

Expected:
```json
{"endpoints": {"streamable_http": "/mcp"}}
```

**FastAPI docs**

Open `http://localhost:8081/docs` in your browser. You should see the Swagger UI with all routers listed.

**Frontend**

Open `http://localhost:5173`. The Agience UI should load. If it shows a blank page, check the browser console for errors (usually a missing backend URL or CORS issue).

**Login flow**

1. Click Sign In
2. Complete Google OAuth
3. You should land in the Agience workspace with your inbox seeded

**MCP endpoint**

```bash
curl -H "Authorization: Bearer <your-token>" \
     http://localhost:8081/mcp
```

Expected: an HTTP 200 or SSE response (not a 401 or 404).

**Database containers**

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
```

All containers (`graph`, `search`) should show `(healthy)`.

---

## 8. Run Tests

### Backend

From `backend/`:

```bash
# Lint
pip install ruff
ruff check .

# Tests
pytest tests/
```

Run a specific suite:

```bash
pytest tests/test_router_workspaces.py
pytest tests/test_service_workspace.py
```

Run with verbose output:

```bash
pytest tests/ -v
```

**Note**: Tests use mocked databases and never connect to live ArangoDB, OpenSearch, or S3. No running containers are required for the test suite.

### Frontend

From `frontend/`:

```bash
# Lint
npm run lint

# Tests (Vitest + React Testing Library)
npm run test

# Tests in watch mode
npm run test -- --watch
```

### Full pre-push gate

Run both suites before pushing a branch. CI enforces the same commands on `main`.

```bash
# Backend
cd backend && ruff check . && pytest tests/

# Frontend
cd frontend && npm run lint && npm run test
```

On Windows PowerShell in two separate steps:

```powershell
Set-Location backend; ruff check .; pytest tests/
Set-Location ..\frontend; npm run lint; npm run test
```

---

## 9. Troubleshooting

### Backend won't start — "database connection refused"

The database containers are not yet healthy. Check:

```bash
docker ps
```

If containers are missing, start them again (section 3). If they are present but not healthy, check their logs:

```bash
docker logs agience-core-graph-1
docker logs agience-core-search-1
```

Common cause: OpenSearch takes 30–60 seconds to initialize on first boot. The backend retries, but if it times out, restart the backend process after OpenSearch becomes healthy.

### OpenSearch fails to start — "max virtual memory areas"

This is a Linux kernel setting. On WSL 2:

```bash
wsl -d docker-desktop
sysctl -w vm.max_map_count=262144
```

To make it permanent, add `vm.max_map_count=262144` to `/etc/sysctl.conf` inside WSL.

### "Access denied" after Google login

Your email is not in the allowlist. Either re-run the setup wizard and add your email, or add it to `.env.local`:

```dotenv
ALLOWED_EMAILS=your-actual-email@gmail.com
```

Restart the backend after editing `.env.local`.

### Frontend shows blank page or API 401 errors

Check that the backend is running at `http://localhost:8081`. If you used `.env.local`, verify `VITE_BACKEND_URI` is set to `http://localhost:8081`. Also verify the JWT in `localStorage` is not expired (12-hour lifetime); log out and back in to refresh it.

### `npm run dev` fails — "Cannot find module"

Node modules are missing or stale:

```bash
cd frontend
rm -rf node_modules package-lock.json   # bash / Git Bash
npm install
```

On Windows PowerShell:

```powershell
Remove-Item -Recurse -Force node_modules, package-lock.json
npm install
```

### `ruff check` reports errors

Fix lint errors before running tests:

```bash
cd backend
ruff check . --fix      # auto-fix safe issues
ruff check .            # verify remaining issues
```

### `pytest` fails with import errors

Your Python path may be off. Run pytest from inside `backend/`:

```bash
cd backend
pytest tests/
```

If that still fails, confirm your venv is active (`which python` on bash, `Get-Command python` on PowerShell) and that `pip install -r requirements.txt` completed without errors.

---

## Related Docs

- [Self-hosting](self-hosting.md) — production/VPS deployment
- [Admin setup](admin-setup.md) — first-admin provisioning
- [Best practices](../guides/best-practices.md) — code style and architecture patterns
