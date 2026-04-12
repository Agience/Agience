# Testing MCP Integration

Status: **Reference**
Date: 2026-04-01

## Smoke test

> **Note**: The smoke test script (`.scripts/mcp_smoke_test.ps1`) is not yet implemented. Use the manual testing flow below to verify MCP connectivity.

---

## Testing with an external MCP server (e.g. GitHub Copilot)

### 1. Register the server as an artifact

External MCP servers are registered by creating an `application/vnd.agience.mcp-server+json`
artifact in the workspace you want to use it from. There is no separate Settings > MCP Servers UI.

Create a workspace artifact (via API or JSON artifact editor) with this context:

```json
{
  "content_type": "application/vnd.agience.mcp-server+json",
  "title": "GitHub Copilot",
  "transport": {
    "type": "http",
    "well_known": "https://api.githubcopilot.com/.well-known/mcp.json",
    "env": {
      "Authorization": "Bearer ghp_YOUR_TOKEN_HERE"
    }
  },
  "notes": "GitHub Copilot MCP server for code assistance"
}
```

The `env` field on HTTP transports is forwarded as **HTTP headers** to the downstream server.

### 2. Get a GitHub token

1. Go to https://github.com/settings/tokens
2. Generate new token (classic) with scopes: `repo`, `read:user`, `read:org`
3. Paste the token in the artifact's `env.Authorization` field.

---

## Manual testing flow

### View tools and resources

1. Open the workspace containing the mcp-server artifact.
2. Trigger `GET /mcp/workspaces/{id}/servers` (or use the UI sidebar MCP section).
3. Expected response: array of `MCPServerInfo` with `tools` and `resources` populated.

### Import a resource as an artifact

```http
POST /artifacts/{server_id}/op/resources_import
Content-Type: application/json
Authorization: Bearer <token>

{
  "workspace_id": "<workspace-id>",
  "resources": [{ "id": "...", "uri": "...", "title": "...", "kind": "..." }]
}
```

Returns `{ "created": 1, "ids": ["..."] }`. Refresh the workspace to see the new artifact.

### Call a tool directly

```http
POST /artifacts/{server_id}/invoke
Content-Type: application/json
Authorization: Bearer <token>

{
  "name": "search",
  "arguments": { "query": "team decisions", "limit": 5 }
}
```

---

## Troubleshooting

### "Failed to list tools/resources"

Check backend logs:
```bash
tail -f logs/backend.log
```
Look for:
- `MCP HTTP communication error:`
- `Failed to discover MCP endpoint:`
- HTTP status codes (401 = auth fail, 404 = wrong URL)

### "Authorization header not being sent"

Verify in browser DevTools → Network tab:
1. Find request to `/mcp/workspaces/{id}/servers`
2. Check backend logs for the actual HTTP request to the external server
3. Headers should include: `Authorization: Bearer ...`

### "Resource import does nothing"

1. Is a workspace active?
2. Check browser console for errors
3. Backend logs should show artifact creation
4. Refresh workspace to see the new artifact
