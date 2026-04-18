# Local MCP Setup (VS Code / Claude Desktop / Cursor)

Status: **Reference**
Date: 2026-04-01

MCP tools are exposed through the Agience MCP server using Streamable HTTP transport via the official `modelcontextprotocol/python-sdk`.

## Endpoints

When the backend is running at `http://localhost:8081`:

- Discovery: `GET http://localhost:8081/.well-known/mcp.json`
- MCP (Streamable HTTP): `http://localhost:8081/mcp`

## Start the stack

Windows (recommended):

- Run `./launch-local.bat`

This starts Docker services (ArangoDB/OpenSearch) plus the backend (`http://localhost:8081`) and frontend (`http://localhost:5173`).

## Auth for MCP clients

Agience accepts `Authorization: Bearer <token>` where `<token>` is either:
- A **JWT** (from OAuth login)
- An **API key** (auto-resolved to user identity by the MCP auth middleware)

For non-interactive/programmatic MCP clients, create a scoped API key in the Agience UI, then use it as the Bearer token.

## Connecting from VS Code

Add to your VS Code `settings.json` (or `.vscode/mcp.json`):

```json
{
  "mcp": {
    "servers": {
      "agience": {
        "type": "http",
        "url": "http://localhost:8081/mcp",
        "headers": {
          "Authorization": "Bearer <your-api-key-or-jwt>"
        }
      }
    }
  }
}
```

## Connecting from Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "agience": {
      "url": "http://localhost:8081/mcp",
      "headers": {
        "Authorization": "Bearer <your-api-key-or-jwt>"
      }
    }
  }
}
```

## Available tools

Once connected, your MCP client will discover these tools:

| Tool | Description |
|---|---|
| `search` | Hybrid search across collections and workspaces |
| `get_artifact` | Retrieve a specific artifact by ID |
| `browse_collections` | List/explore collections and their artifacts |
| `browse_workspaces` | List/explore workspaces and their artifacts |
| `create_artifact` | Create a new artifact in a workspace |
| `update_artifact` | Update an existing workspace artifact |
| `manage_artifact` | Archive, revert, or delete a workspace artifact |
| `extract_information` | Extract structured information from source artifacts |
| `commit_preview` | Preview what a workspace commit would produce |
| `commit_workspace` | Commit workspace artifacts into collections (requires human approval) |
| `ask` | Ask a question with search + LLM synthesis |
| `relay_status` | Check Desktop Host Relay connection status |

## Quick verification

Set your token (JWT or API key) in an env var and run the smoke test:

- PowerShell: `$env:AGIENCE_MCP_TOKEN = "<your token>"`
- Then: `powershell -NoProfile -ExecutionPolicy Bypass -File .scripts/mcp_smoke_test.ps1`

This verifies discovery, tool listing, and a basic tool call against the local backend.

## Manual tool call

To verify a specific tool end-to-end:

```http
POST http://localhost:8081/artifacts/{server_id}/invoke
Content-Type: application/json
Authorization: Bearer <token>

{
  "name": "search",
  "arguments": { "query": "test", "limit": 3 }
}
```

## Troubleshooting

**"Failed to list tools/resources"** — check backend logs for:
- `MCP HTTP communication error:`
- `Failed to discover MCP endpoint:`
- HTTP 401 = auth fail, 404 = wrong URL

**"Authorization header not being sent"** — open browser DevTools → Network tab, find the request to `/mcp/servers`, and confirm the `Authorization` header is present.
