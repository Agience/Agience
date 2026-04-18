# Agent Execution

Status: **Reference**
Date: 2026-04-17

---

## Overview

An **agent** in Agience is a declarative manifest artifact — not a running process or an embedded script. It binds three concerns into a single referenceable artifact: what to execute (an Operator), where to execute it (a host), and who is authorizing execution (an identity). The agent artifact is a recipe pointer that routes work through an MCP server and produces durable, provenance-rich workspace artifacts.

An **operator** is the execution spec that an agent references. It carries the tool wiring, retrieval configuration, and step logic for a particular operation. Operators are also artifacts — they live in the workspace, can be committed to collections, and are auditable. Agents and operators separate *who runs what and where* (agent) from *how the work is done* (operator), so either concern can be changed independently.

---

## Agent artifacts

An agent artifact is stored with `content_type: "application/vnd.agience.agent+json"`. Its `context` field carries the manifest.

### Context shape

```json
{
  "type": "agent",
  "title": "Sage",
  "content_type": "application/vnd.agience.agent+json",
  "agent": {
    "version": 1,
    "order": {
      "artifact_id": "<workspace-operator-artifact-id>"
    },
    "host": {
      "kind": "mcp",
      "server_id": "desktop-host",
      "certified": true,
      "certification": {
        "issuer": "agience",
        "signature": "...",
        "artifact": {
          "kind": "git",
          "url": "https://github.com/org/repo",
          "ref": "commit:abc123"
        }
      }
    },
    "identity": {
      "kind": "user",
      "id": "<user-id>",
      "scopes": ["tool:*:invoke"],
      "meta": {}
    }
  }
}
```

### Field reference

| Path | Required | Description |
|------|----------|-------------|
| `agent.version` | yes | Schema version. Currently `1`. |
| `agent.order.artifact_id` | yes | Reference to the Operator artifact in the same workspace. (`order` is the legacy code field name.) |
| `agent.host.kind` | yes | Compute boundary type. Value: `"mcp"`. |
| `agent.host.server_id` | yes | MCP server identifier — must be attached to the workspace or auto-discoverable. |
| `agent.host.certified` | yes | Whether the host has a valid certification chain. |
| `agent.host.certification` | no | Certification envelope. See [Certification](#certification) below. |
| `agent.identity.kind` | yes | `"user"`, `"service"`, or `"api_key"`. |
| `agent.identity.id` | yes | Authenticated principal ID. |
| `agent.identity.scopes` | yes | Permission scopes for this execution, e.g., `["tool:*:invoke"]`. |
| `agent.identity.meta` | no | Extensible metadata. |

### Notes

- Agent artifacts **never contain secrets**. Connection credentials are resolved per-call via connection artifacts or workspace-attached config.
- `agent.order.artifact_id` (legacy code field name) points to an Operator artifact in the **same workspace**. When committing to a collection, both the Agent and Operator artifacts should be committed together to preserve the reference.
- Agent artifacts follow the standard artifact lifecycle: create in workspace → curate → commit to collection.

### Certification

When `agent.host.certified` is `true`, the `certification` block pins the host to an auditable artifact:

| Field | Description |
|-------|-------------|
| `issuer` | Who certified the artifact (e.g., `"agience"`). |
| `signature` | Cryptographic signature over the artifact reference. |
| `artifact.kind` | Artifact type: `"git"`, `"container"`, `"npm"`, etc. |
| `artifact.url` | Repository or registry URL. |
| `artifact.ref` | Pinned reference: `commit:<sha>`, `digest:<hash>`, or `tag:<name>`. |

Scopes in `identity.scopes` are policy constraints, not capability grants — the host's MCP server process enforces them at the transport layer.

---

## Operator execution

### Execution config block

The Operator artifact `context` may include an optional `execution` block that configures which MCP tools the runner calls:

```json
{
  "execution": {
    "retriever_tool": "sage.azure_docs.retrieve",
    "hydrator_tool": "agience.collection_artifacts.by_version_ids"
  }
}
```

| Field | Description |
|-------|-------------|
| `retriever_tool` | MCP tool name used for retrieval. Typically served by an external MCP server such as Sage. |
| `hydrator_tool` | MCP tool name used for hydration. |

### Defaults

If the Operator does not include an `execution` block, the runner applies these defaults:

| Field | Default value |
|-------|---------------|
| `retriever_tool` | `sage.azure_docs.retrieve` |
| `hydrator_tool` | `agience.collection_artifacts.by_version_ids` |

### Retriever server selection

1. If the caller provides `retriever_server` in the request body, that server is used.
2. Otherwise, Agience auto-discovers a workspace-attached MCP server that exposes the configured `retriever_tool`.

---

## Invocation API

### `POST /artifacts/{id}/invoke`

Agents are artifacts. All execution flows through the unified artifact invoke endpoint. The `{id}` is the artifact UUID of the agent being invoked. **Identity always comes from the auth token (JWT or API key), never from the request body.**

The artifact's `type.json` `operations.invoke` block declares the dispatch handler. `operation_dispatcher.dispatch("invoke", artifact, body, ctx)` resolves the handler, enforces grants, fires lifecycle events, and delegates to `agent_service.invoke()` for agentic execution.

#### Dispatch rules

1. `transform_id` present, `agent` absent → Operator-artifact execution path via `operation_dispatcher`.
2. `agent` present → task-agent dispatch; `params` and `agent_params` are merged; `workspace_id` and `artifacts` are injected into the merged params.
3. Neither present → LLM mode using `input`, `context`, `instructions`, and `capabilities` fields. Returns `{ "output": "<string>" }`.

> **Removed:** `POST /agents/invoke` — superseded by the unified artifact invoke endpoint.

---

## Output artifacts

A successful agent execution writes the following workspace artifacts:

### Evidence artifacts

```
context.type = "evidence"
```

- One artifact per retrieved source item.
- Contains best-effort snippet content and provenance metadata (source reference, retriever tool, version IDs).

### Answer artifact

```
context.type = "answer"
```

- One artifact per run.
- Contains the composed answer text generated by the LLM step.

### Run report artifact

```
context.type        = "run_report"
context.content_type = "application/vnd.agience.run-report+json"
```

- One artifact per run.
- Links the run inputs, the tools invoked, and the IDs of the output artifacts.
- Provides a full audit trail: what ran, with what inputs and tools, producing what outputs.

All output artifacts carry provenance metadata in their `context` field. The run report is the primary artifact for auditing a specific execution.

---

## Multi-tenancy notes

- Agent and Operator artifacts are scoped to a workspace. A run cannot read artifacts from a workspace it is not authorized against.
- Do not embed secrets in Agent or Operator artifact `context`. Resolve connection credentials per-call via connection artifacts or workspace-attached config.
- `agent.identity.scopes` express the *maximum* permissions the execution may use — they are constraints on the host, not grants. The MCP transport layer enforces them at the server process boundary.

---

## See also

- [MCP Overview](../mcp/overview.md) — MCP server model, tool discovery, and transport
- [Layered Architecture](../architecture/layered-architecture.md) — layer boundaries, Handler vs Core
- [Artifact Model](../architecture/artifact-model.md) — artifact lifecycle, context field, reference model
