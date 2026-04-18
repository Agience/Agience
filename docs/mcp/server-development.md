# MCP Server Development

Status: **Reference**
Date: 2026-04-01

This guide is for developers building MCP servers that connect to Agience. It covers every integration path: from a plain MCP server registered by URL, to a fully integrated server that authenticates, calls back to workspaces, and defines its own content types.

---

## Who this is for

An **MCP server developer** builds tools, resources, and viewers that extend Agience beyond what the platform ships. Their server runs independently and is registered in a workspace. The platform calls their tools; their server calls the platform back.

This is the Agience community extensibility model. Everything in this guide is the same contract that the first-party Agience servers (Aria, Sage, Atlas, etc.) follow.

---

## Three tiers of integration

| Tier | What it means | Auth surface |
|------|--------------|--------------|
| **Tier 1 — Outbound** | Platform calls your tools. No Agience-specific server code required. Your server may require credentials FROM the platform — configure via `auth` in the server artifact context. | Optional: platform-to-server auth via `authorizer_id` (OAuth2), `secret_id` (API key), or `static` header |
| **Tier 2 — Bi-directional** | Your server calls Agience APIs; may serve viewers (HTML Views / `ui://`). Server has its own identity. | Server credential (client_credentials grant) or API key for calling Core; delegation JWT from Core for viewer tool proxying |
| **Tier 3 — Fully integrated** | Everything in Tier 2, plus workspace event subscriptions. Server reacts to artifact lifecycle events. | Same as Tier 2 plus event subscription registration |

Start at Tier 1. Add Tier 2 when your tools need to read or write artifacts, or when you want a viewer in the platform UI. Add Tier 3 when your server needs to react to workspace events.

> **Auth direction is not the same at each tier.** Tier 1 is outbound-only from Core's perspective — but your server may still require the platform to authenticate TO it. That credential is configured in the server artifact, separate from server code (see [Tier 1: platform-to-server auth](#tier-1-platform-to-server-auth)). Tier 2 and Tier 3 are bi-directional: your server authenticates to Core with a server credential, and Core authenticates to your server the same way as Tier 1.

---

## Part 1: Tier 1 server — outbound only

### What you need

Any server built with the official MCP SDK is a Tier 1 server with zero Agience-specific code. The platform connects to it as an MCP client and calls your tools and resources just like any other MCP host would. If your server requires credentials, configure them in the server artifact (see [Tier 1: platform-to-server auth](#tier-1-platform-to-server-auth)) — no changes to server code.

**Requirements:**
- Implements the MCP protocol (tools/list, tools/call, etc.)
- Serves over Streamable HTTP or stdio
- Publishes a `.well-known/mcp.json` discovery document (HTTP servers)

**Recommended SDK:** [`modelcontextprotocol/python-sdk`](https://github.com/modelcontextprotocol/python-sdk) via `FastMCP`.

### Minimal Python server

```python
# server.py
from mcp.server.fastmcp import FastMCP
import uvicorn, os

mcp = FastMCP("my-server")

@mcp.tool(description="Return a greeting for the given name.")
async def greet(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=8100)
```

### Discovery document

Publish at `/.well-known/mcp.json`:

```json
{
  "name": "my-server",
  "version": "1.0.0",
  "endpoints": {
    "streamable_http": "/mcp"
  }
}
```

FastMCP does **not** auto-serve this file. Add a plain route alongside your MCP app, or serve the file from your web server / CDN.

---

### Tier 1: platform-to-server auth

Your server may require the platform to authenticate when calling it. This is common for internal or partner servers that enforce bearer tokens or API keys. The credential is declared in the server artifact (see Part 2) — your server code does not change.

Three auth modes are supported:

| Mode | When to use | Config fields |
|------|-------------|--------------|
| `oauth2` | Your server accepts OAuth2 bearer tokens. Credentials are stored per-user and refreshed automatically via an Authorizer artifact. | `authorizer_id` (artifact ID of the Authorizer) |
| `api_key` | Your server accepts a static API key. The key is stored in Agience Secrets. | `secret_id` (secret ID), `header` (header name) |
| `static` | Literal header value. Dev/testing only — do not use in production. | `header`, `value` |

Add an `auth` block at the top level of your server artifact context:

```json
{
  "title": "My Server",
  "transport": {
    "type": "http",
    "well_known": "https://my-server.example.com/.well-known/mcp.json"
  },
  "auth": {
    "type": "api_key",
    "secret_id": "<secret-id-from-POST-/secrets>",
    "header": "Authorization"
  }
}
```

For OAuth2, reference an Authorizer artifact (see Part 4 for how to create one):

```json
{
  "auth": {
    "type": "oauth2",
    "authorizer_id": "<artifact-id-of-authorizer>"
  }
}
```

The platform resolves credentials at call time. Your server code sees only a normal `Authorization` header — no Agience-specific auth logic required.

---

## Part 2: Registering your server in Agience

Agience discovers external servers through workspace artifacts. You never edit a config file — you create an artifact.

### Load from HTTP endpoint

In the Agience UI, create a workspace artifact with content type `application/vnd.agience.mcp-server+json`:

```json
{
  "content_type": "application/vnd.agience.mcp-server+json",
  "title": "My Server",
  "transport": {
    "type": "http",
    "well_known": "https://my-server.example.com/.well-known/mcp.json"
  }
}
```

Agience fetches the discovery document, resolves the MCP endpoint, and immediately makes your tools available in the workspace.

Or via the API:

```http
POST /artifacts
Authorization: Bearer <user-jwt-or-api-key>
Content-Type: application/json

{
  "title": "My Server",
  "content_type": "application/vnd.agience.mcp-server+json",
  "content": "{\"transport\": {\"type\": \"http\", \"well_known\": \"https://my-server.example.com/.well-known/mcp.json\"}}"
}
```

### Load from GitHub URI (planned — requires Desktop Host Relay)

A `github` transport type is planned. It accepts an `owner/repo` reference and runs the server as a sandboxed local subprocess via the Desktop Host Relay:

```json
{
  "content_type": "application/vnd.agience.mcp-server+json",
  "title": "My GitHub Server",
  "transport": {
    "type": "github",
    "owner_repo": "my-org/my-mcp-server",
    "ref": "main"
  }
}
```

When registered, the Desktop Host Relay on the user's machine clones the repository, installs dependencies, and runs the server as a stdio subprocess inside a sandboxed environment. The relay enforces the command allowlist (`npx`, `uvx`, `python`, `python3`, `node`, `deno`) and local policy before executing anything.

**This transport type is not yet implemented.** It depends on the Desktop Host Relay being installed and connected. Until then, the options are:

- Deploy your server to any reachable host and register it with `"type": "http"` (see above).
- Run it locally via the Desktop Host Relay with `"type": "stdio"` (self-hosted deployments with `ALLOW_STDIO_MCP_SERVERS=true` only).

### stdio transport (local / desktop relay)

For servers running locally or via the Desktop Host Relay, use stdio transport. Keep secrets out of committed artifacts:

```json
{
  "content_type": "application/vnd.agience.mcp-server+json",
  "title": "Local Python Server",
  "transport": {
    "type": "stdio",
    "command": "python",
    "args": ["server.py"],
    "env": {
      "MY_API_KEY": "..."
    }
  }
}
```

Secrets in `env` blocks are secured by the platform: they are encrypted at rest via `POST /secrets` and resolved only at tool invocation time. Sharing a collection that contains a stdio server artifact is safe — the artifact carries no raw secrets and is not executable until resolved in the user's own environment with their own credentials.

---

## Part 3: Tier 2 server — bi-directional

When your server needs to call Agience APIs (reading artifacts, creating workspace content, searching collections), it authenticates as itself using the **OAuth 2.0 client credentials grant**. Viewers (HTML Views) also require Tier 2 — the tool proxy path from viewer to Core to your server is described in [Part 5](#part-5-defining-content-types).

### The two credential paths

| Path | Who uses it | How issued |
|------|-------------|------------|
| **Server credential** (provisioned) | Third-party servers, community servers | Via `POST /server-credentials` from an authenticated user |
| **API key** (user-scoped) | Simpler integrations; servers acting on behalf of a user | Via `POST /api-keys` from an authenticated user |

Most community server developers use **server credentials** — they give your server its own identity, scoped permissions, and audit trail that is separate from any individual user.

---

### Server credentials — provisioning

A server credential is a `client_id` / `client_secret` pair that lets your server process request its own access token from Agience. An Agience admin or the workspace owner creates it.

#### Step 1: Register the credential

An authenticated user calls:

```http
POST /server-credentials
Authorization: Bearer <user-jwt>
Content-Type: application/json

{
  "client_id": "my-org-my-server",
  "name": "My Server (Production)",
  "server_id": "my-server",
  "host_id": "my-org-production",
  "scopes": [
    "artifact:read",
    "search:read",
    "artifact:invoke"
  ],
  "resource_filters": {
    "workspaces": "*",
    "collections": "*"
  }
}
```

**Response** (the `client_secret` is returned **once only** — store it immediately):

```json
{
  "client_id": "my-org-my-server",
  "client_secret": "scs_<opaque-secret>",
  "name": "My Server (Production)",
  "server_id": "my-server",
  "host_id": "my-org-production",
  "authority": "agience.ai",
  "scopes": ["artifact:read", "search:read", "artifact:invoke"],
  "created_time": "2026-04-01T12:00:00Z"
}
```

Store `client_id` and `client_secret` in your server's environment or secret manager. The `client_secret` hash is stored; the raw value is never retrievable again.

#### Step 2: Configure your server

Set environment variables on your server process:

```
AGIENCE_API_URI=https://my.agience.ai/api       # or your self-hosted instance
AGIENCE_CLIENT_ID=my-org-my-server
AGIENCE_CLIENT_SECRET=scs_<your-secret>
```

#### Step 3: Exchange for an access token

Call `POST /auth/token` with `grant_type=client_credentials`:

```http
POST /auth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=my-org-my-server
&client_secret=scs_<your-secret>
```

**Response:**

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "expires_in": 3600
}
```

Server tokens expire after **1 hour**. There is no refresh token — exchange credentials again when the token nears expiry.

#### Step 4: Use the token

Pass the access token as a Bearer header on all Agience API calls:

```http
GET /artifacts
Authorization: Bearer <access-token>
```

---

### Token management — reference implementation

Copy this pattern (from the Agience first-party servers) for production-grade token handling:

```python
import asyncio, base64, json, time, os
import httpx

AGIENCE_API_URI = os.getenv("AGIENCE_API_URI").rstrip("/")
CLIENT_ID = os.getenv("AGIENCE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AGIENCE_CLIENT_SECRET")

_token_state: dict = {"access_token": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()


async def get_token() -> str | None:
    """Return a valid access token, refreshing 60 s before expiry."""
    if not CLIENT_SECRET:
        return None

    async with _token_lock:
        if _token_state["access_token"] and time.time() < _token_state["expires_at"] - 60:
            return _token_state["access_token"]

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{AGIENCE_API_URI}/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            body = resp.json()

        token = body["access_token"]
        # Decode expiry from the JWT payload (no verification needed here)
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        _token_state["access_token"] = token
        _token_state["expires_at"] = float(payload.get("exp", time.time() + 3600))
        return token


async def auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = await get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
```

Key points:
- The lock ensures only one concurrent exchange even under load.
- The 60-second pre-expiry buffer prevents race conditions on long tool calls.
- If `CLIENT_SECRET` is not set, `get_token()` returns `None` and requests are unauthenticated — useful during local development without credentials.

---

### API key auth — simpler integrations

If your server acts on behalf of a specific user (rather than as its own identity), an API key is simpler than server credentials.

An API key is created by the user in the Agience UI (`Settings → API Keys`) or via:

```http
POST /api-keys
Authorization: Bearer <user-jwt>
Content-Type: application/json

{
  "name": "My Server Key",
  "scopes": ["artifact:read", "search:read"],
  "resource_filters": {"workspaces": "*", "collections": "*"}
}
```

Use the returned key directly as a Bearer token — no exchange step:

```http
GET /artifacts
Authorization: Bearer <api-key>
```

**Scopes** govern what the key can do:

| Scope | Permission |
|-------|-----------|
| `artifact:read` | Read artifacts (workspaces and collections) |
| `artifact:write` | Create and update artifacts |
| `artifact:manage` | Archive, revert, or delete artifacts |
| `artifact:invoke` | Invoke artifact operations (agents, tools) |
| `search:read` | Search across workspaces and collections |
| `stream:read` | Read live streams |
| `stream:ingest` | Ingest stream data |

---

### Calling back to the Agience API

Once authenticated, your server can call any platform endpoint. The base pattern:

```python
async def call_agience(method: str, path: str, **kwargs):
    headers = await auth_headers()
    async with httpx.AsyncClient() as client:
        resp = await getattr(client, method)(
            f"{AGIENCE_API_URI}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
    resp.raise_for_status()
    return resp.json()


# Search artifacts
results = await call_agience("post", "/artifacts/search", json={"query": "machine learning", "limit": 10})

# Fetch a specific artifact
artifact = await call_agience("get", f"/artifacts/{artifact_id}")

# Create a new artifact (collection_id scopes it to the workspace/collection)
new_artifact = await call_agience("post", "/artifacts", json={
    "collection_id": workspace_id,
    "title": "Analysis Result",
    "content_type": "application/vnd.agience.research+json",
    "content": json.dumps({"summary": "...", "findings": [...]}),
})
```

---

### Acting on behalf of a user

When a tool call is triggered by a human action, your server has its own identity (`Authorization`) but the human is `X-On-Behalf-Of`. Pass both headers:

```python
async def user_auth_headers(user_id: str) -> dict[str, str]:
    headers = await auth_headers()
    headers["X-On-Behalf-Of"] = user_id
    return headers
```

The platform resolves both: access is checked against the server credential's scopes AND the user's grants. An operation that requires user ownership will still fail if the user does not own the resource.

The `user_id` value is the Agience `Person.id` (a UUID) — it is the `sub` claim of the user's JWT. The user's JWT can be passed to your server as a tool argument if needed:

```python
@mcp.tool(description="Create an artifact on behalf of the user.")
async def create_for_user(
    workspace_id: str,
    title: str,
    user_token: str,   # user passes their JWT to the tool
) -> str:
    # Decode sub from the user JWT (not verification — just claim extraction)
    payload_b64 = user_token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    user_id = payload["sub"]

    headers = await user_auth_headers(user_id)
    ...
```

---

### Secret rotation

Rotate a server credential's secret without downtime:

```http
POST /server-credentials/{client_id}/rotate
Authorization: Bearer <user-jwt>
```

Response returns the new `client_secret` once. The old secret is immediately invalidated. Update your server's environment before rotating in production.

---

## Part 4: OAuth connections — server calls external services

If your server needs to access an **external service** (GitHub, Google Drive, Slack, etc.) on behalf of a user, do not hardcode tokens into environment variables. Use Agience OAuth Connections.

> **This is distinct from Tier 1 platform-to-server auth.** Part 4 covers your server (Tier 2) authenticating to a third-party service on behalf of a user. Tier 1 platform-to-server auth covers the platform authenticating to YOUR server when calling it.

### What OAuth Connections are

An **Authorizer** is a provider-specific OAuth configuration stored in Agience (auth URL, token URL, scopes, PKCE settings). A **Connection** is a per-user binding to an authorizer — it stores the user's encrypted access + refresh token. A **Projection** is a just-in-time ephemeral set of request headers produced from a Connection for a single tool call.

Your server never stores or sees credentials. The platform handles token storage, refresh, and injection.

### Registering an authorizer

An authorizer is a workspace artifact of type `application/vnd.agience.authorizer+json`. Create it via the UI or API:

```json
{
  "content_type": "application/vnd.agience.authorizer+json",
  "title": "GitHub (read-only)",
  "content": {
    "provider": "github",
    "auth_url": "https://github.com/login/oauth/authorize",
    "token_url": "https://github.com/login/oauth/access_token",
    "scopes": ["repo:read"],
    "client_id": "<your-github-oauth-app-client-id>",
    "pkce": false
  }
}
```

The `client_secret` for the OAuth app is stored separately via `POST /secrets` (encrypted at rest) and linked to the authorizer by `secret_id`.

### User connects

From your server's tool (or from the Agience UI), start the OAuth flow:

```http
POST /connections/{authorizer_id}/start
Authorization: Bearer <user-jwt>
```

Returns a redirect URL. The user authenticates at the provider. The platform receives the callback at `GET /connections/{authorizer_id}/callback`, exchanges the code, and stores the encrypted token state.

### Using a connection in a tool call

Accept a `connection_id` as a tool parameter. The platform injects the headers:

```python
@mcp.tool(description="List GitHub repos using the user's GitHub connection.")
async def list_github_repos(connection_id: str) -> str:
    # Ask the platform to project credentials for this connection
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/connections/{connection_id}/project",
            headers=await auth_headers(),
        )
    resp.raise_for_status()
    projected = resp.json()   # {"Authorization": "Bearer ghp_...", ...}

    # Use the projected headers for the external API call
    async with httpx.AsyncClient() as client:
        gh_resp = await client.get(
            "https://api.github.com/user/repos",
            headers=projected,
        )
    return json.dumps(gh_resp.json()[:5], indent=2)
```

The projected headers are ephemeral — they are never logged or persisted by the platform.

---

## Part 5: Defining content types

If your server introduces new artifact types (e.g., `application/vnd.my-org.recipe+json`), you define the type inline with your server. The platform registry picks it up when your server is registered.

### Directory layout

```
servers/my-server/
├── server.py
├── requirements.txt
├── pyproject.toml
├── .well-known/
│   └── mcp.json
└── ui/
    └── application/
        └── vnd.my-org.recipe+json/
            ├── type.json       ← type identity + display metadata
            └── view.html       ← iframe viewer (optional)
```

### type.json

```json
{
  "mime": "application/vnd.my-org.recipe+json",
  "version": 1,
  "inherits": ["application/json"],
  "description": "A structured recipe with ingredients and steps.",
  "ui": {
    "label": "Recipe",
    "icon": "chef-hat",
    "color": "#16a34a",
    "viewer": "recipe"
  }
}
```

| Field | Description |
|-------|-------------|
| `mime` | Full MIME type string. Use your domain as the vendor prefix. |
| `version` | Schema version. Increment on breaking context changes. |
| `inherits` | Parent types. Controls fallback rendering. |
| `ui.label` | Human-readable name shown in cards. |
| `ui.icon` | Icon key (Lucide icon name) or emoji. |
| `ui.color` | Hex color for type badge. |
| `ui.viewer` | Key used by the frontend registry to route to the correct viewer. |

### view.html — the iframe viewer

The viewer is a standalone HTML page served as a `ui://` MCP resource. The platform loads it in a sandboxed iframe when a card of this type is opened.

```python
@mcp.resource("ui://my-server/vnd.my-org.recipe.html")
async def recipe_viewer_html() -> str:
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.my-org.recipe+json" / "view.html"
    return view_path.read_text(encoding="utf-8")
```

The HTML file receives the artifact's `content` and `context` via the MCP Apps JSON-RPC protocol (SEP-1865) and renders it. Example viewer skeleton:

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: system-ui, sans-serif; padding: 1rem; }
  </style>
</head>
<body>
  <div id="root"></div>
  <script>
    let _reqId = 1;

    function sendRequest(method, params) {
      const id = _reqId++;
      window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
      return id;
    }

    function sendNotification(method, params) {
      window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");
    }

    window.addEventListener("message", (event) => {
      const msg = event.data;
      if (!msg || msg.jsonrpc !== "2.0") return;

      if (msg.method === "ui/notifications/tool-result") {
        // Initial artifact data delivered after ui/initialize
        const item = (msg.params?.content || [])[0];
        if (item?.type === "text") {
          const artifact = JSON.parse(item.text);
          render(artifact);
        }
      }
    });

    // Initiate the MCP Apps handshake
    sendRequest("ui/initialize", { protocolVersion: "2026-01-26" });

    sendNotification("ui/notifications/initialized", {});

    function render(artifact) {
      const data = typeof artifact.content === "string"
        ? JSON.parse(artifact.content || "{}")
        : (artifact.content || {});
      document.getElementById("root").innerHTML =
        `<h1>${data.title || "Recipe"}</h1>
         <ul>${(data.ingredients || []).map(i => `<li>${i}</li>`).join("")}</ul>`;
    }
  </script>
</body>
</html>
```

> **Auth in viewers:** Viewer iframes are sandboxed and never receive raw tokens. All tool calls from a viewer go via the `tools/call` JSON-RPC method — the platform host proxies them to Core using the user's session, and Core issues a delegation JWT (RFC 8693, `aud=server_client_id`) when forwarding to first-party servers. Your viewer calls tools as normal; credential handling is invisible.

### Lifecycle tools

The platform calls these tool names at known artifact lifecycle events, if your server implements them:

| Tool name | When called | Purpose |
|-----------|-------------|---------|
| `on_create` | Artifact of this type is created | Validate, enrich, set defaults |
| `on_open` | Artifact opened in viewer | Warm caches, fetch live data |
| `extract_text` | Artifact committed to a collection | Return plain-text for search indexing |
| `summarize` | Artifact used as LLM context | Return a condensed summary |

Implement only the ones you need. Unimplemented lifecycle tools are silently skipped.

```python
@mcp.tool(description="Extract plain text from a recipe for full-text indexing.")
async def extract_text(artifact_id: str) -> str:
    artifact = await call_agience("get", f"/artifacts/{artifact_id}")
    data = json.loads(artifact.get("content", "{}"))
    text_parts = [data.get("title", "")]
    text_parts.extend(data.get("ingredients", []))
    text_parts.extend(data.get("steps", []))
    return " ".join(text_parts)
```

---

## Part 6: Server structure reference

### Recommended layout

```
servers/my-server/
├── server.py               ← FastMCP app + tools + resources
├── pyproject.toml          ← package definition (name, version, dependencies)
├── requirements.txt        ← pip-installable dependency list
├── Dockerfile              ← container image for deployment
├── .env.example            ← required environment variables (no secrets)
├── .well-known/
│   └── mcp.json            ← discovery document
├── ui/                     ← content type definitions
│   └── application/
│       └── vnd.my-org.<type>+json/
│           ├── type.json
│           └── view.html
└── tests/
    └── test_tools.py
```

### pyproject.toml

```toml
[project]
name = "my-mcp-server"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.0",
    "fastmcp>=0.1",
    "httpx>=0.27",
    "uvicorn>=0.30",
]
```

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8100
CMD ["python", "server.py"]
```

### .well-known/mcp.json

```json
{
  "name": "my-org-my-server",
  "version": "1.0.0",
  "endpoints": {
    "streamable_http": "/mcp"
  },
  "tools": ["tool_one", "tool_two"],
  "icon": "https://my-server.example.com/logo.png"
}
```

Add an `_meta.icon` to the FastMCP server init for UI display in Agience:

```python
mcp = FastMCP(
    "my-org-my-server",
    instructions="Describe what this server does for LLM context.",
)
```

---

## Part 7: Full integrated server example

A complete server that authenticates, calls back to the workspace, and defines a content type:

```python
# server.py
"""
my-org-my-server — Agience-integrated MCP server.

Tools
-----
  analyze_artifact   — Read an artifact and return analysis
  create_report      — Create a new report artifact in the workspace

Auth
----
  AGIENCE_API_URI        — Platform base URL
  AGIENCE_CLIENT_ID      — Server credential client_id
  AGIENCE_CLIENT_SECRET  — Server credential client_secret

Transport
---------
  MCP_HOST=0.0.0.0
  MCP_PORT=8100
"""

from __future__ import annotations

import asyncio, base64, json, logging, os, pathlib, time
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("my-org-my-server")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

AGIENCE_API_URI = os.getenv("AGIENCE_API_URI")
CLIENT_ID = os.getenv("AGIENCE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AGIENCE_CLIENT_SECRET")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8100"))

# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------

_token_state: dict = {"access_token": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()


async def get_token() -> str | None:
    if not CLIENT_SECRET:
        return None
    async with _token_lock:
        if _token_state["access_token"] and time.time() < _token_state["expires_at"] - 60:
            return _token_state["access_token"]
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{AGIENCE_API_URI}/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            body = resp.json()
        token = body["access_token"]
        padded = token.split(".")[1] + "=="
        payload = json.loads(base64.urlsafe_b64decode(padded))
        _token_state["access_token"] = token
        _token_state["expires_at"] = float(payload.get("exp", time.time() + 3600))
        return token


async def auth_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    token = await get_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "my-org-my-server",
    instructions="Analyze artifacts and create structured reports in the workspace.",
)


@mcp.tool(description="Read an artifact and return an analysis summary.")
async def analyze_artifact(artifact_id: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/artifacts/{artifact_id}",
            headers=await auth_headers(),
            timeout=15,
        )
    if resp.status_code >= 400:
        return f"Error {resp.status_code}: {resp.text[:200]}"
    artifact = resp.json()
    content = artifact.get("content", "")
    return f"Title: {artifact.get('title')}\nLength: {len(content)} chars\nPreview: {content[:300]}"


@mcp.tool(description="Create a report artifact in a workspace from analysis results.")
async def create_report(
    collection_id: str,
    title: str,
    summary: str,
    source_artifact_id: Optional[str] = None,
) -> str:
    body: dict = {
        "collection_id": collection_id,
        "title": title,
        "content_type": "application/vnd.my-org.report+json",
        "content": json.dumps({
            "summary": summary,
            "source_id": source_artifact_id,
        }),
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGIENCE_API_URI}/artifacts",
            headers=await auth_headers(),
            json=body,
            timeout=15,
        )
    if resp.status_code >= 400:
        return f"Error {resp.status_code}: {resp.text[:200]}"
    created = resp.json()
    return f"Created artifact {created['id']}: {created['title']}"


# ---------------------------------------------------------------------------
# Content type viewer resource
# ---------------------------------------------------------------------------

@mcp.resource("ui://my-org-my-server/vnd.my-org.report.html")
async def report_viewer_html() -> str:
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.my-org.report+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    log.info("Starting my-org-my-server on %s:%s", MCP_HOST, MCP_PORT)
    uvicorn.run(mcp.streamable_http_app(), host=MCP_HOST, port=MCP_PORT)
```

---

## Part 8: Server credential management reference

| Operation | Endpoint | Notes |
|-----------|----------|-------|
| Register | `POST /server-credentials` | Returns `client_secret` once |
| List | `GET /server-credentials` | No secrets returned |
| Get | `GET /server-credentials/{client_id}` | No secret returned |
| Update | `PATCH /server-credentials/{client_id}` | Name, scopes, filters, active status |
| Rotate secret | `POST /server-credentials/{client_id}/rotate` | Old secret immediately invalidated; returns new secret once |
| Delete | `DELETE /server-credentials/{client_id}` | Permanent |

All credential management endpoints require a **human JWT** (not a server credential or API key). Servers cannot self-register or self-rotate.

### Scopes reference

| Scope | What it grants |
|-------|---------------|
| `artifact:read` | Read artifact metadata and content (workspaces and collections) |
| `artifact:write` | Create and update artifacts |
| `artifact:manage` | Archive, revert, or delete artifacts |
| `artifact:invoke` | POST /artifacts/{id}/invoke |
| `search:read` | Search across workspaces and collections |
| `stream:read` | Read live stream artifacts |
| `stream:ingest` | Write to stream artifacts |

Use the narrowest scopes your server needs. Credentials are audited — every token exchange records `last_used_at`.

### Resource filters

Resource filters restrict which specific workspaces or collections the credential can access. Use `"*"` for unrestricted:

```json
{
  "resource_filters": {
    "workspaces": ["ws_id_1", "ws_id_2"],
    "collections": "*"
  }
}
```

---

## Part 9: Testing

### Unit testing tools

Mock the Agience API — never hit the real platform in unit tests:

```python
# tests/test_tools.py
import pytest
from unittest.mock import AsyncMock, patch
from server import analyze_artifact

@pytest.mark.asyncio
async def test_analyze_artifact_returns_summary():
    mock_artifact = {
        "id": "abc123",
        "title": "Test Doc",
        "content": "Hello world content",
    }
    with patch("server.auth_headers", return_value={}), \
         patch("httpx.AsyncClient.get", return_value=AsyncMock(
             status_code=200, json=lambda: mock_artifact
         )):
        result = await analyze_artifact("ws1", "abc123")
    assert "Test Doc" in result
    assert "19 chars" in result
```

### Integration testing against a local stack

Run a local Agience stack (`./launch-local.bat`), create a server credential via the API, then call your tools directly:

```bash
# Create a server credential
curl -X POST http://localhost:8081/server-credentials \
  -H "Authorization: Bearer <your-user-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"client_id":"test-server","name":"Test","server_id":"test","host_id":"local","scopes":["artifact:read"],"resource_filters":{"workspaces":"*","collections":"*"}}'

# Export credentials
export AGIENCE_API_URI=http://localhost:8081
export AGIENCE_CLIENT_ID=test-server
export AGIENCE_CLIENT_SECRET=scs_<returned-secret>

# Run your server
python server.py

# Register it in a workspace (create the artifact), then invoke a tool
curl -X POST http://localhost:8081/artifacts/<server-artifact-id>/invoke \
  -H "Authorization: Bearer <user-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name":"analyze_artifact","arguments":{"workspace_id":"<ws-id>","artifact_id":"<art-id>"}}'
```

### Smoke test checklist

- [ ] Server starts and responds to `GET /.well-known/mcp.json`
- [ ] `tools/list` returns your tools with correct descriptions and schema
- [ ] Token exchange succeeds (`POST /auth/token` with client credentials)
- [ ] A tool call that reads an artifact returns expected content
- [ ] A tool call that creates an artifact confirms it appears in the workspace
- [ ] Token auto-refreshes when within 60 s of expiry (set a short `expires_in` in test env)
- [ ] A tool call with an invalid token returns an appropriate error (not a crash)

---

## Summary: which integration path to use

| Scenario | Recommended path |
|----------|-----------------|
| Expose third-party tools to a workspace (no Agience API calls) | Standard server, HTTP or stdio transport |
| Server needs to search or read artifacts | Server credential, `artifact:read` scope |
| Server creates artifacts in the workspace | Server credential, `artifact:write` scope |
| Server acts on behalf of a specific user | API key (user-scoped) or server credential + `X-On-Behalf-Of` |
| Server needs to access Google/GitHub/Slack on behalf of user | OAuth Connections + Authorizer artifact |
| Server defines its own artifact types with custom viewers | `ui/` directory + `@mcp.resource("ui://...")` + `type.json` |

---

## Related reading

- [MCP Overview](overview.md) — how Agience uses MCP as a client
- [MCP Client Setup](client-setup.md) — connecting your local tools to Agience
- [Security Model](../architecture/security-model.md) — JWT shapes, grants, transport binding
- [Content Type Registry](../features/content-type-registry.md) — how types are discovered and routed
- [Architecture Overview](../architecture/overview.md) — the three-layer platform model
