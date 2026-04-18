# Desktop Host Relay

Status: **Reference**
Date: 2026-04-01

The Desktop Host Relay lets the Agience cloud invoke MCP tools and access resources on a user's local machine without requiring inbound ports. From the browser, local tools appear and behave identically to any other MCP tool call.

**What you get:**
- Cloud-to-local tool calls that feel identical to any remote MCP server call.
- No inbound ports required on the user machine — works behind NAT and corporate firewalls.
- A safe, policy-enforced baseline for sensitive local capabilities (filesystem, process execution).
- A single installable companion that can also supervise other local MCP servers.

---

## Architecture

```
Browser
  └── POST /artifacts/{server_id}/invoke  ──→  Cloud (policy enforcement)
                                                └── invoke_tool  ──→  Desktop Host (WebSocket)
                                                                        └── executes tool locally
                                                                        └── returns tool_result
```

**How it works:**
1. Desktop Host runs locally and authenticates to Cloud using an Agience-issued JWT.
2. Desktop Host opens an outbound WebSocket relay channel to `wss://<cloud>/relay/v1/connect`.
3. Cloud treats Desktop Host as an MCP server and routes tool invocations over the relay.
4. Desktop Host executes tools locally, enforces local policy, and returns results.

The channel is **bidirectional**: Cloud invokes Desktop Host tools, and Desktop Host can push events, resource imports, and derived artifacts back to Cloud over the same connection.

### Desktop Host as local MCP supervisor

Desktop Host can also supervise local stdio MCP server processes, acting as a bridge between Cloud and tools that are not directly network-accessible.

Supervisor responsibilities:
- Maintain a local registry of pre-approved MCP server definitions (command, args, cwd, env).
- Start, stop, and restart subprocesses; health check and capture logs.
- Proxy `tools/list`, `tools/call`, `resources/list`, and `resources/read` between Cloud and each local server.

**Key safety constraint:** Desktop Host never starts a process not already in the local allowlist. Cloud may request lifecycle actions, but Desktop Host validates every request against local policy.

Proxying local MCP calls uses the normal `invoke_tool` message with a namespaced `server_id` (e.g., `local-mcp:<server_id>`).

---

## Authentication

Desktop Host authenticates with an Agience-issued JWT obtained through the upstream OIDC login.

**Auth flow:**
- Desktop Host uses an OIDC device flow or local browser loopback redirect.
- Refresh token is stored in the OS credential vault; access tokens are short-lived.
- Desktop Host presents a stable OAuth `client_id` so its sessions are distinguishable from browser sessions.

Cloud verifies:
- JWT signature via Agience JWKS.
- `sub` as the owning Agience user.
- `client_id` as the Desktop Host runtime identity.
- Token audience/scope appropriate for relay.

Desktop Host is modeled as a **client acting on behalf of a user**, not a separate human principal.

**Request headers on WebSocket upgrade:**
```
Authorization: Bearer <agience_access_token>
X-Device-Id: <stable-device-id>          (optional)
X-Agience-Client: desktop-host/<version>  (optional)
```

---

## Installation

Target experience: user downloads one installer, clicks through, signs in once, and the app runs in the background.

**Platform packaging:**

| Platform | Format |
|----------|--------|
| Windows | Signed `.exe` (NSIS/WiX/MSIX) |
| macOS | Notarized `.dmg` or `.pkg` with hardened runtime |
| Linux | AppImage, `.deb`, or `.rpm` |

**First-run flow:**
1. "Sign in" button opens browser-based IdP login (device flow or loopback redirect).
2. Desktop Host stores refresh token in OS credential vault.
3. Desktop Host connects to Cloud relay and shows connection status.

**Operational features:**
- Optional start-on-login toggle.
- Visible Pause / Disconnect control.
- Diagnostics page: last-seen timestamp, log export, version.
- Auto-update via signed update mechanism.

---

## Authorization and policy

Two enforcement layers operate in sequence. Cloud is the first; Desktop Host is the second and cannot be bypassed by Cloud.

**Cloud policy (Agience scopes):**
- Controls which workspaces and tools are visible and invokable.
- Validates grant existence and expiry when a guest user is present.
- Enforces rate limits and quotas.

**Desktop Host local policy:**
- Filesystem access limited to user-configured allowed roots.
- Write and delete operations may require explicit per-call or per-session consent.
- Tool allowlist is maintained locally; requests for tools not on the list are rejected.

This dual enforcement prevents a compromised Cloud token from reading arbitrary files off disk.

---

## Built-in tool surface

**Filesystem tools:**
- `fs.list_dir` — read-only, restricted to allowed roots
- `fs.read_text` — read-only, restricted to allowed roots
- `fs.write_text` — write, restricted to allowed roots, requires consent

**Supervisor tools:**
- `mcp.servers.list_local`
- `mcp.servers.start_local`
- `mcp.servers.stop_local`

**Resources:**
- `file://<relative>` resources representing allowlisted roots

---

## Pushing information to Cloud

Desktop Host is not only an execution target — it can also push information into Cloud.

**Resource import (recommended):** Desktop Host exposes local items as resources (`desktop://clipboard`, `file://...`, `desktop://recent-files`). The user or an automation imports selected resources into a workspace as artifacts. This follows the standard Agience pattern: external truth → pull/import into workspace → curate → commit.

**Events:** Desktop Host sends signals such as "file changed" or "job complete". Cloud records the event and may create a workspace artifact or trigger an operator.

**Uploads:** Small text content is sent inline; large or binary artifacts use a presigned S3 upload URL requested from Cloud (same security model as browser uploads).

**Safety rules:** Desktop Host must not exfiltrate arbitrary filesystem content. Any automatic push must be scoped to allowlisted roots and require explicit user consent. Cloud records provenance (which host, when, from which local source).

---

## Security model

| Threat | Mitigation |
|--------|-----------|
| Token theft on desktop | Short-lived access tokens + refresh token in OS vault |
| Cloud compromise | Desktop Host local policy prevents unsafe operations regardless of Cloud instruction |
| Confused deputy / workspace mixup | `workspace_id` included in every relay request; Desktop Host validates it |
| Write/delete abuse | Rate limits + consent prompts |
| Arbitrary command execution | Desktop Host only starts pre-approved processes from the local allowlist |

All relay traffic is over TLS. Payload size limits are enforced. Every invocation is audited with owner, guest, grant_id, tool name, and timestamps.

---

## Protocol reference

### Transport

WebSocket at `wss://<cloud>/relay/v1/connect`

### Message envelope

All messages are JSON objects:

```json
{
  "type": "ping",
  "v": 1,
  "id": "2f4d...",
  "ts": 1711929600,
  "payload": {"nonce": "abc"}
}
```

| Field | Description |
|---|---|
| `type` | Message type identifier |
| `v` | Protocol version (current: `1`) |
| `id` | Unique per message (UUID recommended) |
| `ts` | Unix seconds (optional) |
| `payload` | Message-specific content |

### Connection lifecycle

1. **Connect** — Desktop Host opens WebSocket. Cloud sends `server_hello`.
2. **Register** — Desktop Host sends `client_hello`.
3. **Heartbeat** — Either side may send `ping`; peer replies `pong`. Recommended interval: 30 seconds.
4. **Reconnect** — Desktop Host reconnects with exponential backoff. Cloud includes `session_id` in `server_hello`; Desktop Host may present `resume_session_id` to resume.

### Message types

#### `server_hello` — Cloud → Desktop Host

Payload: `session_id`, `server_time`, `features`

#### `client_hello` — Desktop Host → Cloud

Payload: `device_id`, `display_name`, `capabilities: { "tools": boolean, "resources": boolean }`, `capabilities_manifest` (optional)

#### `ping` / `pong`

Keepalive. No payload required.

#### `invoke_tool` — Cloud → Desktop Host

| Field | Description |
|---|---|
| `request_id` | Correlation ID for this tool call |
| `owner_user_id` | Owning Agience user |
| `guest_user_id` | Null for owner-only calls; required for shared calls |
| `grant_id` | Null for owner-only calls; required for shared calls |
| `workspace_id` | Workspace context |
| `server_id` | e.g., `desktop-host` or `desktop-host:<owner>` |
| `tool_name` | Tool to invoke |
| `arguments` | Tool arguments |
| `deadline_ms` | Timeout deadline |

#### `tool_result` — Desktop Host → Cloud

| Field | Description |
|---|---|
| `request_id` | Matches the `invoke_tool` request |
| `ok` | Success boolean |
| `result` | Result object (if ok) |
| `error` | Error object with `code`, `message`, `details` (if not ok) |

Error codes: `TIMEOUT`, `DENIED`, `NOT_FOUND`, `INVALID_ARGUMENT`

#### `cancel_tool` — Cloud → Desktop Host

Payload: `request_id`, `reason` (optional). Desktop Host makes a best-effort attempt to cancel in-progress work.

### Local MCP supervision messages

#### `list_local_servers` — Cloud → Desktop Host

No payload. Response via `tool_result` with `servers: [{ server_id, label, status }]`

#### `start_local_server` / `stop_local_server` — Cloud → Desktop Host

Payload: `server_id` — must exist in local allowlist.

#### `log_event` — Desktop Host → Cloud (optional)

Payload: `level`, `message`, `context`

### Tool call semantics

**Cloud enforces:** authenticated caller, grant existence/expiry/allowlist, rate limits.

**Desktop Host enforces:** local filesystem roots, byte limits, read-only mode, tool allowlist.

**Timeout:** Cloud provides `deadline_ms`. Desktop Host must stop work when exceeded and return a `TIMEOUT` error.

**Idempotency:** `request_id` should be treated as an idempotency key for retry handling.
