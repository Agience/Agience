# Quickstart: Agience Hosted

Status: **Reference**
Date: 2026-04-01

Goal: connect an MCP client, run a search, create an artifact, and commit it — under 10 minutes.

---

## Before You Begin

You need:

- An Agience account (hosted preview — request access at agience.ai if not yet allowlisted)
- A supported MCP client: VS Code with Copilot, Claude Desktop, or Cursor
- An Agience API key (generated from the Agience UI after login)

**Get your API key**: After logging in, open Settings → API Keys → Create Key. Give it a name (e.g., "vs-code-dev") and copy the key. You will use it as a Bearer token in your MCP client config.

---

## Step 1: Connect to MCP

Agience exposes an MCP server at `/mcp` using Streamable HTTP transport. Add the following config to your MCP client.

### VS Code

Add to `.vscode/mcp.json` (or your workspace `settings.json` under `"mcp"`):

```json
{
  "mcp": {
    "servers": {
      "agience": {
        "type": "http",
        "url": "https://my.agience.ai/api/mcp",
        "headers": {
          "Authorization": "Bearer <your-api-key>"
        }
      }
    }
  }
}
```

Replace `<your-api-key>` with the key you copied from Settings.

### Claude Desktop

Add to `claude_desktop_config.json` (typically at `~/Library/Application Support/Claude/` on macOS or `%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "agience": {
      "url": "https://my.agience.ai/api/mcp",
      "headers": {
        "Authorization": "Bearer <your-api-key>"
      }
    }
  }
}
```

### Verify the connection

Once your client loads the config, it should discover the Agience tool set. In Claude Desktop, you will see tools like `search`, `create_artifact`, and `commit_preview` in the available tools list. In VS Code Copilot, `@agience` becomes available as an MCP participant.

---

## Step 2: Try It — Search

Before creating anything, check what is already in your workspace and the platform collections.

Run this tool call from your MCP client:

```json
{
  "tool": "search",
  "arguments": {
    "query": "getting started"
  }
}
```

**What to expect**: Results from your Inbox workspace and the platform Getting Started collection (both provisioned automatically on first login). Each result includes an artifact ID, title, content snippet, and whether it came from a workspace or a collection.

Collection results are authoritative — they have been committed and versioned. Workspace results are drafts.

Try a topic-specific search:

```json
{
  "tool": "search",
  "arguments": {
    "query": "architecture decisions",
    "size": 5
  }
}
```

The `size` parameter limits result count. You can also scope search to a specific workspace or collection using `workspace_ids` or `collection_ids`.

---

## Step 3: Try It — Create an Artifact

Create a simple text artifact in your Inbox workspace. You need your workspace ID for this step.

**Get your workspace ID**: Run `browse_workspaces` to list your workspaces:

```json
{
  "tool": "browse_workspaces",
  "arguments": {}
}
```

The response lists your workspaces with their IDs. Your Inbox workspace will be listed first. Copy its `workspace_id`.

**Create an artifact**:

```json
{
  "tool": "create_artifact",
  "arguments": {
    "workspace_id": "<your-workspace-id>",
    "content": "Agience uses ArangoDB for all artifact storage: both ephemeral workspace state and committed collection artifacts.",
    "context": {
      "title": "Architecture note",
      "type": "note",
      "content_type": "text/plain",
      "tags": ["architecture", "database"]
    }
  }
}
```

**What to expect**: The response includes the new artifact's `id` (a UUID), its `state` (`"draft"`), and the metadata you provided. The artifact is now in your workspace as a draft.

The `context` object carries all metadata — title, tags, semantic kind, and provenance. Keep artifacts atomic: one concept per artifact, not a long document.

---

## Step 4: Try It — Commit

Committing promotes workspace artifacts into a collection. The process is two steps: preview, then commit. The server requires a token from the preview — you cannot skip to commit directly.

**Step 4a: Preview the commit**

```json
{
  "tool": "commit_preview",
  "arguments": {
    "workspace_id": "<your-workspace-id>"
  }
}
```

The response shows which artifacts would be committed, to which collection, and any warnings (e.g., artifacts with no title). It also returns a `commit_token`. Copy this token — it expires after 30 minutes.

Example response shape:

```json
{
  "collection_id": "...",
  "collection_name": "Personal",
  "artifacts_to_commit": [
    {
      "id": "<artifact-id>",
      "title": "Architecture note",
      "state": "draft"
    }
  ],
  "commit_token": "<token>"
}
```

**Step 4b: Review and confirm**

Inspect the preview. If the plan looks correct, proceed. If not, you can archive or edit artifacts in the workspace first, then run `commit_preview` again for a fresh token.

**Step 4c: Commit**

```json
{
  "tool": "commit_workspace",
  "arguments": {
    "workspace_id": "<your-workspace-id>",
    "commit_token": "<token-from-preview>"
  }
}
```

**What to expect**: The response confirms which artifacts were committed and their new version IDs. The artifact state moves from `"draft"` to `"committed"` in the workspace. The artifact is now in your Personal collection as a versioned, searchable record.

You can verify it appears in search:

```json
{
  "tool": "search",
  "arguments": {
    "query": "ArangoDB architecture"
  }
}
```

The committed artifact should appear with a collection source, not workspace.

---

## Commit Rules (Important)

The commit step is intentionally gated. The server enforces these constraints:

- `commit_workspace` requires a `commit_token` from a preceding `commit_preview`. Calls without a valid token are rejected with 400.
- Tokens expire after 30 minutes. If expired, run `commit_preview` again.
- Only principals with appropriate grants (e.g., can_invoke on the workspace) can commit. Unauthorized attempts return 403.
- `commit_workspace` carries `destructiveHint: true` in the MCP protocol — compliant clients will flag this as requiring human confirmation.

Never call `commit_workspace` autonomously in an agent loop. Always preview first, present the plan, and wait for explicit human approval.

---

## Quick Reference: All Available Tools

| Tool | What it does |
|---|---|
| `search` | Hybrid semantic + keyword search across workspaces and collections |
| `get_artifact` | Fetch the full content of an artifact by ID |
| `browse_collections` | List collections or see artifacts in a specific collection |
| `browse_workspaces` | List workspaces or see artifacts in a specific workspace |
| `ask` | Search + LLM synthesis in one call — answers a question grounded in your knowledge base |
| `create_artifact` | Create a new artifact in a workspace |
| `update_artifact` | Edit an existing workspace artifact |
| `manage_artifact` | Archive, revert, or delete a workspace artifact |
| `extract_information` | Decompose a source artifact into structured knowledge units |
| `commit_preview` | Preview a commit and get a `commit_token` (read-only, safe to call anytime) |
| `commit_workspace` | Commit workspace artifacts into a collection (requires token + human approval) |
| `relay_status` | Check Desktop Host Relay connection status for the current user |

---

## Next Steps

- **Self-hosting**: Run Agience on your own infrastructure — see [self-hosting.md](self-hosting.md).
- **MCP local setup**: Connect a local development instance to VS Code or Claude Desktop — see [client-setup.md](../mcp/client-setup.md).
- **Architecture**: Understand the platform design — see [../overview/platform-overview.md](../overview/platform-overview.md).
- **Features**: Search query language, workspace automation, agent execution — see [../features/](../features/).
- **MCP client instructions**: Full reference for how agents should use Agience tools — see [../mcp/client-instructions.md](../mcp/client-instructions.md).
