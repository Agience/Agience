# MCP Overview

Status: **Reference**
Date: 2026-04-01

Agience is MCP-native in two directions:

- it exposes Agience knowledge and curation capabilities through its own MCP server
- it connects to external MCP servers and brings those tools and resources into a workspace

The MVP goal is practical interoperability, not protocol novelty. Agience uses MCP to make workspaces, collections, search, curation, and automation available across clients such as VS Code, Claude Desktop, and other MCP-compatible tools.

For local client configuration, see [MCP Client Setup](./client-setup.md).

---

## What Agience exposes over MCP

Agience ships three MCP-facing capabilities:

1. **Agience as an MCP server** at `/mcp`, exposing purpose-driven tools and resources.
2. **Agience as an MCP client**, able to call external MCP servers registered inside a workspace.
3. **Task-agent and workflow plumbing** that uses MCP-compatible tool execution while keeping durable outputs in artifacts.

The product boundary is important: MCP is used for access and execution, while reviewed knowledge still lives in artifacts, workspaces, and collections.

### Design boundaries

- **Commit** remains a human-in-the-loop governance boundary — it is not exposed via MCP as an autonomous operation.
- **Raw CRUD** is not exposed — MCP tools are purposeful actions, not a mirror of every internal endpoint.
- **UI concerns such as artifact reorder** are product-specific interactions, not protocol features.

---

## Core architecture

| Component | Role |
|---|---|
| MCP transport | Streamable HTTP at `/mcp` using the official MCP Python SDK |
| Agience MCP server | Exposes search, artifact access, curation, and analysis tools to external clients |
| Agience MCP client | Connects to external MCP servers over HTTP or stdio |
| Auth layer | RS256 JWTs and scoped API keys for user and service access |
| Storage layer | ArangoDB workspaces and collections, OpenSearch search, S3/CloudFront content |

### Discovery and auth

- Discovery is published at `/.well-known/mcp.json`.
- MCP requests use `Authorization: Bearer <token>`.
- The bearer token can be a JWT or an API key.
- API keys can be used directly or exchanged through `POST /api-keys/exchange`.

---

## Agience as an MCP server

Agience exposes purpose-driven tools rather than raw entity CRUD.

### Knowledge and research tools

| Tool | Description |
|---|---|
| `search` | Hybrid semantic and keyword search across collections and workspaces |
| `get_artifact` | Retrieve a specific artifact by ID |
| `browse_collections` | List accessible collections and optionally their artifacts |
| `browse_workspaces` | List accessible workspaces and optionally their artifacts |
| `ask` | Search plus LLM synthesis grounded in the knowledge base |

### Workspace curation tools

| Tool | Description |
|---|---|
| `create_artifact` | Create a new artifact in a workspace |
| `update_artifact` | Update an existing workspace artifact |
| `manage_artifact` | Archive, revert, or delete a workspace artifact |

### Analysis tools

| Tool | Description |
|---|---|
| `extract_information` | Extract structured information from source artifacts |

### Commit tools

| Tool | Description |
|---|---|
| `commit_preview` | Preview what a workspace commit would produce (read-only) |
| `commit_workspace` | Commit workspace artifacts into collections (requires human approval) |

### Utility tools

| Tool | Description |
|---|---|
| `relay_status` | Check Desktop Host Relay connection status |

### Resources exposed by Agience

| URI Pattern | Description |
|---|---|
| `agience://collections/{id}` | Collection metadata plus artifact list |
| `agience://workspaces/{id}` | Workspace metadata plus artifact list |

### Collections as resources

Collections are exposed through MCP Resources as read-only context.

- `resources/list` returns the collections a caller can access.
- `resources/read` returns collection metadata and a truncated artifact list as JSON.
- Resource reads are intentionally separate from writes.
- Mutations still happen through tools such as `create_artifact`, `update_artifact`, and `manage_artifact`.

This follows the MCP split cleanly:

- **Resources** are for discoverable, readable context.
- **Tools** are for actions and mutations.

### Current resource limitations

- Parameterized resource URIs such as per-artifact collection resource paths are not yet implemented.
- Large resource reads may be truncated because `resources/read` does not currently paginate.
- Collection creation and update are not available through MCP; they remain part of the explicit commit and governance model.

---

## Agience as an MCP client

Agience can call external MCP servers from within a workspace.

### Artifact-native server registration

External servers are registered by creating a workspace artifact with content type `application/vnd.agience.mcp-server+json`.

Example HTTP transport:

```json
{
  "content_type": "application/vnd.agience.mcp-server+json",
  "title": "My MCP Server",
  "transport": {
    "type": "http",
    "well_known": "https://example.com/.well-known/mcp.json"
  },
  "notes": "optional description"
}
```

Example stdio transport:

```json
{
  "content_type": "application/vnd.agience.mcp-server+json",
  "title": "Local Server",
  "transport": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@my/mcp-server"],
    "env": { "API_KEY": "..." }
  }
}
```

Sensitive values in `env` should stay out of committed shared artifacts and should be used only for local or controlled setups.

### Live capability introspection

| Endpoint | Purpose |
|---|---|
| `GET /mcp/servers` | Live tools/resources for all accessible MCP servers |
| `GET /mcp/workspaces/{id}/servers` | Live tools/resources scoped to a workspace |

### Server operations via artifact router

Tool invocation, resource reads, and resource imports flow through the unified artifact operations surface:

| Endpoint | Purpose |
|---|---|
| `POST /artifacts/{server_id}/invoke` | Invoke a tool on an MCP server |
| `POST /artifacts/{server_id}/op/resources_read` | Read a resource from an MCP server |
| `POST /artifacts/{server_id}/op/resources_import` | Import an MCP resource as a workspace artifact |

`server_id` is the artifact UUID of a `vnd.agience.mcp-server+json` artifact. Built-in persona servers are also addressable by their artifact IDs.

### Official-first rule

For external platforms, Agience prefers the official vendor MCP server whenever one exists.

| Use case | Recommended source |
|---|---|
| GitHub | Official GitHub MCP server |
| Filesystem | Official filesystem MCP server |
| AWS | Official AWS MCP servers |
| Slack, Gmail, Notion, Linear | Official vendor MCP servers |

Agience-owned MCP capabilities stay focused on Agience context such as workspace scope, artifact persistence, provenance, and access control. They do not re-implement entire vendor APIs.

---

## MCP and task agents

Agience also uses MCP-compatible execution patterns internally for task agents and automation.

### Unified invocation model

`POST /artifacts/{id}/invoke` is the generic execution entry point.

It supports two MVP modes:

- **Task-agent mode** using a named agent callable
- **LLM mode** using direct model-backed synthesis

The request combines four concerns:

- **Agent**: what should run
- **Knowledge**: which workspace and artifacts form the context
- **Input**: free text or structured params
- **Identity**: who is making the request

### Artifacts-in, artifacts-out

Task agents follow an artifacts-in, artifacts-out contract:

- they receive artifact IDs and workspace scope as input context
- they return proposed actions such as create, update, delete, or log
- services decide whether those actions are previewed or applied

This keeps automation auditable and durable. Even when MCP tools are involved, the product outcome is still expressed in artifacts rather than ephemeral chat-only output.

### Workspace extensions and human review

Workspaces support two relevant extension surfaces:

- attached collections
- registered MCP server artifacts

Agience can suggest attachment changes through structured agent responses, but the user still confirms them. That keeps MCP discovery and automation aligned with the broader human-in-the-loop product posture.

---

## Icon metadata extension

Agience supports icon metadata for MCP servers and items using the MCP `_meta` extension field.

### Hierarchy

1. Generic MCP icon fallback
2. Server-level icon
3. Item-level icon for a tool, resource, or prompt

### Server-level example

```json
{
  "serverInfo": {
    "name": "my-server",
    "version": "1.0.0",
    "_meta": {
      "icon": "https://example.com/logo.png"
    }
  }
}
```

### Item-level example

```json
{
  "resources": [
    {
      "uri": "file:///docs/readme.md",
      "name": "README",
      "_meta": { "icon": "📄" }
    }
  ]
}
```

Supported icon values include image URLs, data URIs, emoji, and the special `agience` marker for Agience-branded rendering.

---

## Security and governance

- Access is governed by JWT or API key scopes.
- Tool invocation is allowlisted and environment-dependent.
- External server access is workspace-scoped rather than globally attached to a user account.
- Commits remain outside MCP because they are governance actions, not background protocol calls.
- Provenance and durable artifact outputs remain the system of record.

### Relevant scopes

- `artifact:read`
- `artifact:write`
- `artifact:manage`
- `search:read`
- `stream:read`
- `stream:ingest`

---

## Related reading

- [Server Development](server-development.md) — build and register MCP servers that integrate with Agience
- [MCP Local Setup](client-setup.md)

