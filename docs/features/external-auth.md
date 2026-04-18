# External Authentication

Status: **Reference**
Date: 2026-04-01

Agience supports upstream authentication via external identity providers (OIDC) and optional email/password. Regardless of which IdP authenticates the user, Agience remains the authorization domain for workspace and collection access, MCP tool/resource scopes, invite grants, and audit identity claims.

Supported identity flows:
- Google / Microsoft Entra / Auth0 / custom OIDC
- Email + password (for self-hosted deployments)

---

## How it works

Users authenticate against the upstream IdP. Agience receives the IdP token, maps it to an internal `Person` record, and issues its own RS256 JWT. All Agience API calls use the Agience-issued token — not the upstream IdP token.

This keeps authorization semantics consistent (shares, grants, internal scopes) while still accepting enterprise IdPs.

### Token types

| Token | Description |
|---|---|
| **Agience access token** | Returned by `POST /auth/token`. Used by browser/desktop to call Agience APIs. Short-lived (12 hours). |
| **Agience refresh token** | Returned alongside the access token. Exchanged at `POST /auth/token` with `grant_type=refresh_token`. Valid 30 days. |
| **Scoped grant token** | Narrowed token for sharing and programmatic access. Carries `api_key_id`, `scopes`, and `resource_filters`. |

---

## JWT claims

### User tokens

| Claim | Meaning |
|---|---|
| `sub` | Agience internal user UUID (`Person.id`) |
| `email`, `name`, `picture` | User profile claims from the upstream IdP |
| `client_id` | The OAuth client or agent/server identity that requested this token |
| `iss` | `AUTHORITY_ISSUER` |
| `iat` / `exp` | Issue and expiry time |

### Scoped/exchanged machine tokens

| Claim | Meaning |
|---|---|
| `sub` | Owning Agience user UUID |
| `client_id` | API key name (traceable in audit logs) |
| `api_key_id` | Back-reference to the API key record |
| `scopes` | Capability list for tool/resource access |
| `resource_filters` | Allowed workspaces or collections |

### Client identity

The `client_id` claim answers *which application requested this token* — distinguishing browser sessions, Desktop Host sessions, VS Code extension sessions, and exchanged agent tokens even when they all belong to the same user. Use stable, application-level identifiers:

- `agience-frontend`
- `vscode-mcp`
- `desktop-host`

### Identity mapping

Agience maps upstream IdP identity to an internal `Person`:
- `person.id` — stable Agience internal user ID
- `person.oidc_provider` + `person.oidc_subject` — upstream identity reference
- Emails are normalized to lowercase before storage.

---

## Configuration

### Provider discovery

`GET /auth/providers` returns configured upstream providers and whether password auth is enabled.

### Environment variables

**Google:**
```
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
GOOGLE_OAUTH_REDIRECT_URI
```

**Microsoft Entra:**
```
MICROSOFT_ENTRA_TENANT
MICROSOFT_ENTRA_CLIENT_ID
MICROSOFT_ENTRA_CLIENT_SECRET
MICROSOFT_ENTRA_REDIRECT_URI
```

**Auth0:**
```
AUTH0_DOMAIN
AUTH0_CLIENT_ID
AUTH0_CLIENT_SECRET
AUTH0_REDIRECT_URI
```

**Custom OIDC:**
```
CUSTOM_OIDC_NAME
CUSTOM_OIDC_METADATA_URL
CUSTOM_OIDC_CLIENT_ID
CUSTOM_OIDC_CLIENT_SECRET
CUSTOM_OIDC_REDIRECT_URI
CUSTOM_OIDC_SCOPES
```

**Password auth:**
```
PASSWORD_AUTH_ENABLED=true|false
PASSWORD_MIN_LENGTH          (default: 12)
PASSWORD_PBKDF2_ITERS        (default: 200000)
```

### Redirect allowlisting

Agience validates `redirect_uri` in `/auth/authorize`:

- In local/dev environments (when `BACKEND_URI` or `FRONTEND_URI` is localhost), common localhost redirects are allowed automatically.
- In non-local deployments, only the explicitly configured `FRONTEND_URI` and `BACKEND_URI` bases are allowed.

### Client registration

Each first-party client should use a stable Agience-side `client_id` in the PKCE flow. Redirect URIs must match the client's actual callback surface. Do not use per-user or per-device values as `client_id`; keep runtime identity stable.

---

## Desktop Host authentication

Desktop Host authenticates through the same OIDC flow as the browser:

- Use loopback redirect or device authorization grant.
- Store refresh tokens in the OS keychain/vault.
- Present a distinct `client_id` (e.g., `desktop-host`) so host sessions are distinguishable from browser sessions in audit logs.
- The authenticated user still appears in `sub`; the host is modeled as a client acting on behalf of that user.

---

## Non-interactive agents and API keys

Agents and servers that cannot perform the browser OIDC flow use the API key exchange path:

1. Create a scoped API key in the Agience UI.
2. Exchange it at `POST /auth/token` for a short-lived Agience JWT.
3. The exchanged token keeps `sub` as the owning user, sets `client_id` to the API key name, and carries `scopes` and `resource_filters`.

This makes agent identity visible in audit trails while keeping execution under a user-owned authorization boundary.

---

## Production deployment notes

- `/auth/authorize` and `/auth/token` use an in-memory cache with TTL and max size for auth state and authorization codes. For multi-instance deployments, back this with Redis or another shared cache so callbacks can land on any instance.
- Rate limiting is not built into the auth router. Deploy behind an API gateway or WAF for brute-force protection.

---

## See also

- [Security Model](../architecture/security-model.md) — grants, scopes, API keys, and collection access control
- [Admin Setup](../getting-started/admin-setup.md) — first-run provider configuration
