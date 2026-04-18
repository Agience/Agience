# Security Model

Status: **Reference**
Date: 2026-04-01

This document is the single reference for Agience's security model. Audience: developers integrating authentication, operators configuring an Agience deployment, and security reviewers assessing trust boundaries.

This document consolidates the authentication, authorization, and credential management architecture.

---

## Overview

Agience has two types of principals and two types of bearer tokens.

**Principals:**

| Type | What it represents | Token used |
|------|--------------------|------------|
| **User** | A human authenticated via an external IdP | RS256 JWT (Agience-issued) |
| **Server** | An MCP server process authenticated as itself | RS256 JWT (Agience-issued, via client credentials grant) |

There is also a third bearer credential type — **API keys** — used for scoped programmatic access on behalf of a user. API keys are not JWTs; they are opaque secrets that are validated by hash lookup and optionally exchanged for a short-lived JWT that carries scoped claims.

Every request that flows through the platform carries a full identity chain: authority (the domain), host (the compute), server (the code), user (the person), and client (the application). This chain is what makes every transaction auditable.

---

## User Authentication Flow

Agience does not authenticate users directly. It delegates identity verification to an external **Identity Provider** (IdP) via OIDC, then issues its own tokens for all subsequent authorization.

```
User
  → External IdP (Google / Entra / Auth0 / custom OIDC / password)
  → IdP authenticates and returns OIDC claims
  → Agience maps claims to internal Person record
  → Agience issues RS256 JWT access token (12-hour expiry)
  → Agience issues refresh token (30-day expiry)
  → Browser/client uses Agience JWT for all API calls
```

The frontend discovers configured providers via `GET /auth/providers`. The interactive OAuth2 PKCE flow runs through `/auth/authorize` and `/auth/token`. Refresh is via `POST /auth/token` with `grant_type=refresh_token`.

**Person mapping.** Agience maintains an internal `Person` record for each user. The record stores `oidc_provider` + `oidc_subject` as the stable binding to the upstream identity. Emails are normalized to lowercase. The Agience-assigned `Person.id` (a UUID) is the `sub` claim in all issued tokens — not the upstream IdP subject.

**Client identity.** The OAuth flow requires a `client_id` parameter that identifies the calling application (e.g., `agience-frontend`, `vscode-mcp`, `desktop-host`). This is copied into the issued JWT as the `client_id` claim, not to be confused with the upstream IdP subject. It allows audit logs to distinguish a browser session, a VS Code session, and a Desktop Host session that all belong to the same `sub`.

**Access token expiry.** Access tokens expire after 12 hours. There is no silent background refresh — the user (or client) must exchange their refresh token to get a new access token. Refresh tokens are valid for 30 days.

**Supported providers:**

| Provider | Required env vars |
|----------|------------------|
| Google | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` |
| Microsoft Entra | `MICROSOFT_ENTRA_TENANT`, `MICROSOFT_ENTRA_CLIENT_ID`, `MICROSOFT_ENTRA_CLIENT_SECRET`, `MICROSOFT_ENTRA_REDIRECT_URI` |
| Auth0 | `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `AUTH0_REDIRECT_URI` |
| Custom OIDC | `CUSTOM_OIDC_NAME`, `CUSTOM_OIDC_METADATA_URL`, `CUSTOM_OIDC_CLIENT_ID`, `CUSTOM_OIDC_CLIENT_SECRET`, `CUSTOM_OIDC_REDIRECT_URI`, `CUSTOM_OIDC_SCOPES` |
| Password (MVP/self-host) | `PASSWORD_AUTH_ENABLED=true`, `PASSWORD_MIN_LENGTH` (default 12), `PASSWORD_PBKDF2_ITERS` (default 200000) |

---

## JWT Claim Shapes

### User Access Token

Issued by `POST /auth/token` after successful IdP authentication or refresh token exchange.

| Claim | Source | Notes |
|-------|--------|-------|
| `sub` | `Person.id` (Agience UUID) | Not the upstream IdP subject |
| `email` | Identity provider | Normalized to lowercase |
| `name` | Identity provider | |
| `picture` | Identity provider | |
| `roles` | Computed from grants | |
| `client_id` | OAuth `client_id` param | Identifies the calling application |
| `iss` | `AUTHORITY_ISSUER` config | |
| `iat` / `exp` | Issue / expiry time | 12-hour expiry |

When a user's API key is exchanged for a machine JWT (see API Keys below), the resulting token additionally carries:

| Claim | Present in | Notes |
|-------|-----------|-------|
| `api_key_id` | Exchanged machine tokens | Back-reference to the API key record |
| `scopes` | Exchanged machine tokens | Capability list for tool/resource access |
| `resource_filters` | Exchanged machine tokens | Resource constraints (workspace IDs, collection IDs) |

### Server Credential Token

Issued by `POST /auth/token` with `grant_type=client_credentials`, used by MCP server processes.

| Claim | Value / Source | Notes |
|-------|---------------|-------|
| `sub` | `server/{client_id}` | e.g., `server/agience-server-seraph` |
| `aud` | `"agience"` | |
| `principal_type` | `"server"` | Distinguishes from user tokens |
| `authority` | Credential authority | Domain authority |
| `host_id` | Credential host | Which compute is running this server |
| `server_id` | Credential server | Which server code (e.g., `seraph`) |
| `client_id` | Credential client ID | e.g., `agience-server-seraph` |
| `scopes` | Credential scopes | Granted capabilities |
| `resource_filters` | Credential resource filters | Resource constraints |
| `iss` | `AUTHORITY_ISSUER` | |
| `iat` / `exp` | Issue / expiry time | 1-hour expiry; no refresh token issued |

Server tokens expire after 1 hour. Servers re-authenticate automatically via the `_exchange_token()` function in `servers/_shared/agience_server_auth.py`. No refresh token is issued — servers simply perform a new client credentials exchange when their token nears expiry (60-second refresh buffer).

When a server makes a request on behalf of a user (e.g., a tool call triggered by a human action), Core issues a **delegation JWT** (see below) that the server presents directly. The delegation JWT carries both the user identity (`sub`) and the server identity (`aud`, `act.sub`).

---

## API Keys

API keys provide scoped programmatic access to the platform, used by MCP clients, agents, and automation that act on behalf of a user. They are distinct from server credentials (which authenticate a server process as itself).

**Key format.** API keys are opaque secrets stored as bcrypt hashes. The raw secret is shown once at creation time and never again.

**Scope format.** `resource|tool|prompt : mime : action`  — for example `tool:*:invoke` (invoke any tool) or `resource:application/vnd.agience.workspace+json:read` (read workspace resources).

**Transport policy.** Each API key carries a `transport_policy` that restricts which network origins may use it:

| Policy | Behavior | Use Case |
|--------|----------|----------|
| `"any"` | No transport check (default) | Cloud/public API keys |
| `"local"` | Loopback only (`127.0.0.0/8`, `::1`) | Claude Code, local dev tools, desktop relay |
| `"network"` | Specified CIDR ranges only | VPN-bound keys, office networks |

Transport policy enforcement happens in `MCPAuthMiddleware` after successful token validation. A valid key used from the wrong network origin returns 403 (not 401) — authentication succeeded but the transport context was denied.

See the [Transport Binding](#transport-binding) section for deployment considerations.

**Exchange flow.** An API key can be presented directly as a bearer token (`Authorization: agience-key {secret}`) or exchanged for a short-lived JWT at `POST /auth/token` with `grant_type=api_key`. The exchanged JWT embeds `api_key_id`, `scopes`, and `resource_filters` so downstream services can enforce policy without a database lookup per request.

**Resource filters.** A key can be restricted to specific workspaces or collections via `resource_filters`. Combined with grants (see below), this is the mechanism for least-privilege access: a key scoped to `tool:*:invoke` but filtered to a single workspace can only invoke tools against that workspace.

---

## Server Credentials

Server credentials are the identity mechanism for MCP server processes authenticating **as themselves** — not on behalf of a user. They use the standard OAuth 2.0 client credentials grant (RFC 6749 §4.4).

**Registration.** Each server has a well-known `client_id` (e.g., `agience-server-seraph`) baked into its `.env`. The `client_id` is not a secret — it appears in configs, logs, and artifact definitions. The `client_secret` is issued per host deployment and stored in the host's secret manager.

**Well-known client IDs:**

| Server | `client_id` |
|--------|-------------|
| aria | `agience-server-aria` |
| sage | `agience-server-sage` |
| atlas | `agience-server-atlas` |
| nexus | `agience-server-nexus` |
| astra | `agience-server-astra` |
| verso | `agience-server-verso` |
| seraph | `agience-server-seraph` |
| ophan | `agience-server-ophan` |

**Kernel fast-path.** All first-party servers authenticate via `PLATFORM_INTERNAL_SECRET` — a shared secret configured in the platform's env. This avoids the auth recursion problem (Core cannot call Seraph to authenticate Seraph) and simplifies deployment (no provisioned `ServerCredential` records needed for builtin servers). Server IDs are derived from `BUILTIN_MCP_SERVER_PATHS` into `KERNEL_SERVER_IDS` in `backend/core/config.py`. The token endpoint checks for kernel server IDs first; if matched, it validates against the internal secret rather than performing a database lookup.

**Standard path.** Third-party servers use the provisioned `ServerCredential` flow: the token endpoint looks up the `ServerCredential` record in ArangoDB and validates the submitted `client_secret` against the stored bcrypt hash.

**ServerCredential entity.** Each registered server has a `ServerCredential` record in ArangoDB carrying: `client_id`, `name`, `secret_hash`, identity chain fields (`authority`, `host_id`, `server_id`), `scopes`, `resource_filters`, `user_id` (the person who registered it), and lifecycle fields. Server credentials are a separate entity from API keys — they authenticate the server as a principal, not as a proxy for a person.

---

## Delegation JWTs (Server ↔ Core User Context)

When Core proxies a user request to an MCP server (e.g., a tool call), it issues a short-lived **delegation JWT** (RFC 8693 token exchange pattern). This token carries both the user's identity and an explicit binding to the target server:

| Claim | Value | Purpose |
|-------|-------|---------|
| `sub` | User ID (the human) | Who the request is on behalf of |
| `aud` | Target server's `client_id` | Which server this token is issued TO |
| `act.sub` | Server's `client_id` | Which server is acting |
| `principal_type` | `"delegation"` | Distinguishes from user/server tokens |
| `iss` | `AUTHORITY_ISSUER` | Core authority |
| `exp` | Current time + 300s | Short TTL — single request lifetime |

**Security properties:**

- **Audience binding**: Servers verify `aud == self.client_id` before accepting a delegation JWT. A token issued to server A cannot be used by server B. This prevents token forwarding between servers.
- **Short TTL**: 300-second expiry limits blast radius of token theft.
- **RS256 signature**: Tokens are verified against Core's JWKS public key, fetched at server startup.
- **Per-request ContextVar**: The verified delegation JWT is stored in an ASGI middleware ContextVar, not passed as a tool argument. Tools access user context via `_auth.get_delegation_user_id()`.

**Flow:**

```
Browser → Core: POST /artifacts/{id}/invoke (user JWT)
Core → MCP Server: tools/call (delegation JWT in Authorization header)
MCP Server: middleware verifies aud == self.client_id, stores in ContextVar
MCP Server → Core: REST callback (delegation JWT in Authorization header)
```

---

## Server Auth Module

All first-party MCP servers use the shared `AgieceServerAuth` class from `servers/_shared/agience_server_auth.py`. No server implements its own JWKS fetch, JWT verification, RSA key management, or ASGI middleware.

**Capabilities:**

| Method | Purpose |
|--------|---------|
| `startup()` | Fetch Core JWKS + register server RSA public key |
| `create_app()` | Wrap MCP app with delegation JWT middleware + startup hooks |
| `verify_delegation_jwt()` | Verify RS256 signature + `aud` + `principal_type` + expiry |
| `verify_core_jwt()` | Verify RS256 signature + expiry only (for operator tokens) |
| `decrypt_jwe()` | Decrypt RSA-OAEP-256 + AES-256-GCM JWE envelopes |
| `user_headers()` | Return delegation JWT (or fallback server token) as auth headers |
| `get_delegation_user_id()` | Extract `sub` from the stored delegation JWT |

**Server key registration.** At startup, each server registers its RSA public key with Core via `PUT /server-credentials/{client_id}/key`. Core uses this key to wrap secrets in JWE envelopes destined for that server.

---

## JWE Secret Delivery

Secrets (OAuth tokens, API credentials) are delivered to MCP servers encrypted. Core wraps each secret in a JWE envelope using the target server's registered RSA public key (RSA-OAEP-256 + AES-256-GCM). Only the target server's private key can decrypt. Plaintext secrets never transit the network.

**Flow:**

```
Server → Core: POST /secrets/fetch (delegation JWT + secret name in body)
Core: encrypts secret with server's registered RSA public key
Core → Server: { "jwe": { "ek": "...", "iv": "...", "ct": "...", "tag": "..." } }
Server: _auth.decrypt_jwe(jwe) → plaintext secret
```

Currently used by Seraph for credential management. Available to any server that registers its RSA public key at startup.

---

## Grants

Grants are the authorization layer that controls collection (and workspace) access. A grant is a server-side record stored in ArangoDB that binds a principal (user, API key, or invite) to a resource with explicit permissions.

**Grant entity fields:**

| Field | Type | Description |
|-------|------|-------------|
| `resource_type` | `str` | `"artifact"` |
| `resource_id` | `str` | The collection or workspace ID |
| `grantee_type` | `str` | `"user"` \| `"api_key"` \| `"invite"` |
| `grantee_id` | `str` | User ID, API key ID, or hashed invite token |
| `can_create` | `bool` | Create permission |
| `can_read` | `bool` | Read permission |
| `can_update` | `bool` | Update permission |
| `can_delete` | `bool` | Delete permission |
| `can_evict` | `bool` | Evict permission |
| `can_add` | `bool` | Add permission |
| `can_share` | `bool` | Share permission |
| `can_invoke` | `bool` | Invoke permission |
| `can_admin` | `bool` | Admin permission |
| `requires_identity` | `bool` | If true, anonymous presenters are rejected |
| `state` | `str` | `"active"` \| `"revoked"` \| `"pending_accept"` |
| `granted_by` | `str` | User ID of the principal who issued the grant |
| `expires_at` | `Optional[str]` | Null = no expiry |

**Access check.** Every request touching a collection runs `check_access()` (from `services/dependencies.py`). All access requires explicit grant records; `created_by` is provenance only and does not imply ownership or access. The function resolves a grant by matching the principal's identifiers against active, non-expired grants on the requested resource. A 404 is returned for both "not found" and "no access" — security by obscurity for collection IDs.

**Relation to API keys.** Authentication (who you are) and authorization (which collections you can reach) are decoupled. Revoking a grant removes collection access without invalidating the API key for other resources.

**Invite grants.** When a collection is shared via a link, the platform issues a grant with `grantee_type="invite"`. The raw token is shown once; only its hash is stored. The share link URL carries the raw token; the server resolves it to the grant on presentation.

**First-login provisioning.** When a new user first logs in, platform collections (authority, inbox, etc.) are made accessible by automatically creating grants for the new user. This is handled by `seed_content_service.py`.

Grants are stored in ArangoDB with three indexes: `(resource_type, resource_id, state)` for listing all grants on a collection, `(grantee_id, resource_type, state)` for listing all grants for a principal, and a sparse `expires_at` index for expiry queries.

---

## OAuth Connections

OAuth Connections are the mechanism for **outbound** access to external provider APIs — Google, Slack, Jira, GitHub, AWS, etc. They are separate from IdP authentication and exist to replace "paste a long-lived token into MCP env" with a managed, revocable, least-privilege credential layer.

**Key concepts:**

- **Authorizer**: the provider-specific OAuth configuration (auth URL, token URL, scopes, PKCE requirements).
- **Connection**: a first-class credential binding per user. Stores an encrypted `token_state` blob (refresh token + access token). Never stores plaintext secrets.
- **Projection**: just-in-time production of ephemeral request headers for a single tool call, derived from a Connection. The headers are never persisted.

**Outbound flow:**

```
User clicks "Connect to Google Drive"
  → POST /connections/google/start → returns IdP authorization URL
  → User authenticates at Google
  → GET /connections/google/callback → exchanges code, stores encrypted token state
  → Tool calls include connection_id
  → Platform decrypts token state, refreshes if needed, injects Authorization header
  → Tool call proceeds with ephemeral credentials
```

**Inbound connections.** Connections also serve inbound verification — validating JWT grant tokens from external publishers (streaming gateways, webhook senders). An inbound connection stores public key material or a JWKS URL for signature verification rather than OAuth token state.

**Security properties:** tokens are encrypted at rest with `DATA_ENCRYPTION_KEY`; tools request `connection_id` and receive ephemeral headers, they never own credentials; connection use is auditable (which connection was used, for which tool call).

---

## JWKS Endpoint

Agience publishes its RSA public key at:

```
GET /.well-known/jwks.json
```

Key management is handled by `backend/core/key_manager.py`. RSA key pairs are generated and stored on disk in `backend/keys/`. The JWKS endpoint publishes the public key(s) so external services and MCP clients can verify Agience-issued JWTs without contacting the platform.

**Key rotation.** The platform can serve multiple keys simultaneously (identified by `kid`) to support zero-downtime rotation. Tokens issued before rotation carry the old `kid`; they remain valid until expiry. New tokens carry the new `kid`. Once all old tokens have expired, the old key can be removed from the JWKS.

---

## Access Control Configuration

Agience supports allowlist-based access control at the platform level, restricting who can create an account or log in. These are configured via environment variables in `backend/core/config.py`:

| Variable | Description |
|----------|-------------|
| `ALLOWED_EMAILS` | Comma-separated list of email addresses permitted to authenticate |
| `ALLOWED_DOMAINS` | Comma-separated list of email domains permitted to authenticate |
| `ALLOWED_GOOGLE_IDS` | Comma-separated list of Google subject IDs permitted to authenticate |

These checks run during the token issuance step, after IdP authentication succeeds. A user whose email or domain is not on the allowlist receives a 403 at `POST /auth/token` regardless of whether their IdP credentials were valid.

For open deployments (no allowlist), leave all three variables unset. For self-hosted single-team deployments, `ALLOWED_DOMAINS` is typically the simplest configuration.

**Rate limiting.** Rate limiting is not implemented at the router level. For brute-force protection, deploy Agience behind an API gateway or WAF.

**Multi-instance deployments.** The `/auth/authorize` + `/auth/token` flow keeps authorization codes and auth state in an in-memory cache with TTL. For multi-instance production deployments, back this with Redis so callbacks can land on any instance.

---

## Summary: Token Types and Their Roles

| Token | Who presents it | How issued | Expiry | Scopes / constraints |
|-------|----------------|------------|--------|----------------------|
| User JWT | Browser, Desktop Host | POST /auth/token (OIDC flow) | 12 hours | `roles` from grants |
| Refresh token | Browser, Desktop Host | Alongside user JWT | 30 days | Exchange-only |
| Exchanged machine JWT | MCP client, agent | POST /auth/token (api_key grant) | Short-lived | `scopes`, `resource_filters` from API key |
| Server JWT | MCP server process | POST /auth/token (client_credentials) | 1 hour | `scopes`, `resource_filters` from ServerCredential |
| Delegation JWT | MCP server (from Core) | Issued by Core when proxying user request | 300 seconds | `sub` = user, `aud` = target server |
| API key (direct) | MCP client, automation | Created via POST /api-keys | Until revoked | `scopes`, `resource_filters` |
