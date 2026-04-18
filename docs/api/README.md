# Agience API Reference

Status: **Reference**
Date: 2026-04-01

---

## Overview

The Agience backend is a FastAPI application. The REST API is fully documented via an auto-generated OpenAPI 3.1 specification. Every endpoint, request schema, response shape, and error code is included.

The API is the primary integration surface for clients that need to manage workspaces, collections, artifacts, agents, search, and authentication outside of the Agience frontend.

---

## Access the Spec

Three built-in interfaces are available on any running Agience backend instance:

| Interface | Path | Description |
|-----------|------|-------------|
| Raw OpenAPI JSON | `GET /openapi.json` | Machine-readable spec — use this to generate client SDKs or static docs |
| Swagger UI | `GET /docs` | Interactive browser UI — browse endpoints, inspect schemas, send test requests |
| ReDoc | `GET /redoc` | Readable rendered reference — good for reading the full spec in one page |

All three are served by FastAPI automatically and require no additional configuration.

---

## Generate a Static Reference

For publishing or offline use, generate a static HTML reference from the OpenAPI spec.

### Using Redoc CLI

```bash
npx @redocly/cli build-docs http://localhost:8000/openapi.json -o docs/api/index.html
```

### Using Scalar

```bash
npx @scalar/cli document http://localhost:8000/openapi.json --output docs/api/index.html
```

### Using Mintlify

If your docs use Mintlify, point the OpenAPI source at the spec URL or a downloaded copy:

```json
{
  "openapi": "https://my.agience.ai/openapi.json"
}
```

Or download the spec and check it in:

```bash
curl https://my.agience.ai/openapi.json -o docs/api/openapi.json
```

---

## Authentication

All API endpoints require a Bearer token in the `Authorization` header:

```
Authorization: Bearer <token>
```

Two token types are accepted:

**User JWT** — issued after OAuth2 login via `POST /auth/token` or the OAuth callback flow. Valid for 12 hours. Carries user identity, roles, and `client_id`.

**API key** — scoped key issued for MCP servers and agents. Scope format: `resource|tool|prompt : mime : action`. API keys do not expire on a fixed schedule but can be revoked. Use scoped keys for programmatic access and server-to-server calls.

To obtain an API key:
1. Open the Agience workspace.
2. Create a `vnd.agience.key+json` artifact with the desired scope.
3. The raw key is shown once on creation and then hashed — store it immediately.

For server-to-server identity (MCP servers authenticating to Agience), use the `client_credentials` grant via `POST /auth/token`. First-party servers use `PLATFORM_INTERNAL_SECRET`; third-party servers use registered `ServerCredential` entity credentials. See `docs/architecture/security-model.md` for details.

---

## Base URLs

| Environment | Base URL |
|---|---|
| Hosted preview | `https://my.agience.ai/api` |
| Self-host local | `http://localhost:8081` (backend service port) |

All paths in the OpenAPI spec are relative to the base URL. The spec itself is available at `<base>/openapi.json`.

---

## Key Endpoint Groups

| Router | Path prefix | Purpose |
|--------|------------|---------|
| `auth_router` | `/auth` | OAuth2 login, token exchange, JWKS |
| `artifacts_router` | `/artifacts`, `/workspaces`, `/collections`, `/search` | Unified artifact API — CRUD, invoke (`POST /artifacts/{id}/invoke`), op dispatch, upload, commit, search. All execution (agents, tools, servers, operators) routes through artifact invoke. Replaces the retired `workspaces_router`, `collections_router`, `agents_router`, and `search_router`. |
| `mcp_router` | `/mcp` | MCP client live discovery (list servers). Tool invocation and resource ops flow through the generic `POST /artifacts/{server_id}/invoke` and `/artifacts/{server_id}/op/{op_name}` dispatch endpoints. |
| `api_keys_router` | `/api-keys` | Scoped API key lifecycle (create, list, revoke, exchange) |
| `secrets_router` | `/secrets` | Encrypted credential CRUD |
| `events_router` | `/events` | Unified real-time events WebSocket |
| `grants_router` | `/grants` | Grant management |
| `types_router` | `/types` | Content-type definitions from the `types/` directory |
| `relay_router` | `/relay` | Desktop host relay sessions (WebSocket) |
| `server_credentials_router` | `/server-credentials` | Server identity CRUD for MCP server `client_credentials` authentication |
| `platform_router` | `/platform` | Platform admin endpoints — settings, users, seed collections. Guarded by `require_platform_admin` (write grant on authority collection). Merged from the retired `admin_router` + `operator_router` on 2026-04-06. |

Full endpoint details including request schemas, query parameters, and response shapes are in the interactive docs at `GET /docs`.

---

## MCP Tools vs REST API

Agience exposes two integration surfaces. Use the right one for your use case:

**REST API** — use when you need to:
- Manage workspaces, collections, and artifacts programmatically
- Build integrations that create, update, or retrieve artifacts
- Implement an auth flow or API key lifecycle
- Access search from a non-MCP client

**MCP tools** — use when you need to:
- Connect an AI agent or LLM to Agience knowledge and operations
- Use Agience from VS Code, Claude Desktop, or any MCP-compatible client
- Invoke Operator artifacts and agentic workflows from a model context
- Access the purpose-built tool surface optimized for LLM consumption

The MCP tool surface is documented in the [MCP Overview](../mcp/overview.md). The MCP server itself is mounted at `POST /mcp` (Streamable HTTP transport) and advertised via `/.well-known/mcp.json`.

The REST API and MCP tools are not redundant — MCP tools are higher-level, workspace-scoped, and designed for model consumption. The REST API exposes the full platform surface at the resource level.
