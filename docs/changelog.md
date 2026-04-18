# Changelog

Status: **Reference**
Date: 2026-04-01

All notable changes to Agience are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.0.0-mvp] — 2026-04-01

### Added

**Core platform**

- Artifact-based knowledge model with durable IDs, content, context metadata, and provenance fields
- Workspaces (ArangoDB) as ephemeral draft staging areas with card-based UI for humans and artifact-based API surface for agents
- Collections (ArangoDB) as versioned, immutable committed knowledge stores
- Explicit commit lifecycle — workspace draft to collection canonical — with commit preview, diff, and review
- ArangoDB as the sole database for both workspaces and collections

**Search**

- Hybrid BM25 (lexical) + kNN (semantic) search with Reciprocal Rank Fusion (k=60)
- OpenAI text-embedding-ada-002 embeddings via OpenSearch
- Aperture control — statistical elbow method for semantic neighborhood filtering
- Field boost presets (description-first, balanced, content-heavy)
- Unified search across workspaces and collections via `POST /artifacts/search`

**AI-assisted workflows**

- `extract_units` agent — LLM-based extraction of candidate artifacts from raw content
- Operator artifacts (`vnd.agience.transform+json`) — reusable multi-step workflow definitions
- Agent dispatch via unified `POST /artifacts/{id}/invoke` endpoint
- WorkspaceEventHandler — event-driven automations triggered by artifact lifecycle events (`artifact_created`, `artifact_updated`, `artifact_deleted`)
- Chat artifacts (`vnd.agience.chat+json`) — agentic multi-turn sessions with full tool-call history

**Ingestion**

- Browser upload for documents and media with direct-to-S3 presigned PUT flow
- PDF text extraction via Astra `document_text_extract` tool
- File URL ingestion via Astra `ingest_file` tool

**MCP integration**

- Agience as MCP server — 12 purpose-built tools exposed at `POST /mcp` (Streamable HTTP)
- Agience as MCP client — workspace-scoped proxy to external MCP servers registered as `vnd.agience.mcp-server+json` artifacts
- VS Code and Claude Desktop compatibility via MCP client connection artifacts
- Desktop host relay — local MCP server bridge via WebSocket for development workflows
- Official-first integration rule — vendor MCP servers registered as artifacts, not reimplemented

**First-party MCP servers**

- **Aria** (port 8083) — response formatting, chat turn execution, presentation artifacts
- **Astra** (port 8087) — file ingestion, text extraction
- **Atlas** (port 8085) — provenance tracing, conflict detection, contract enforcement
- **Nexus** (port 8086) — email/message delivery, MCP server management, sandboxed shell execution
- **Sage** (port 8084) — hybrid search, artifact lookup, Azure AI Search projection
- Unified host mount (`_host`, port 8082) — all persona servers on a single port for deployment simplicity

**Content types (Agience-owned)**

- `application/vnd.agience.chat+json` — chat conversation artifacts
- `application/vnd.agience.view+json` — configurable live workspace views
- `application/vnd.agience.stream+json` — RTMP stream source configuration
- `application/vnd.agience.mcp-server+json` — external MCP server registration
- `application/vnd.agience.mcp-client+json` — MCP client connection configuration
- `application/vnd.agience.host+json` — Agience host configuration

**Platform MIME renderers** (built into the frontend)

- `text/markdown`, `text/plain`, `application/json`, `application/pdf`
- `image/*`, `audio/*`, `video/*`

**Authentication and access control**

- Multi-provider OAuth2: Google, Microsoft, Auth0, custom OIDC, password
- RS256 JWT tokens with JWKS endpoint (`/.well-known/jwks.json`)
- Scoped API keys — `resource|tool|prompt : mime : action` format
- Server credential `client_credentials` grant for MCP server identity
- Collection grants (CRUDEASIO 9-flag model) for shared knowledge access
- Allowed-email / allowed-domain / allowed-ID access control

**Deployment**

- Docker Compose stack: all services, infra (ArangoDB, OpenSearch, MinIO), and MCP persona servers
- Self-hosting path with documented environment variable configuration
- Hosted preview environment at `agience.ai`
- Frontend runtime config injection (`public/config.js`) for zero-rebuild environment switching

### Architecture

- **Three-layer model** — Core (kernel), Handlers (MCP servers and drivers), Presentation (frontend shell) with enforced separation
- **Type blindness in Core** — no MIME constants, no `artifact.context` parsing in Core services or Presentation components
- **Registry-driven viewer dispatch** — all type-specific viewer wiring flows through `frontend/src/registry/`; Presentation never imports handler code directly
- **MCP Apps pattern** — content type viewers served as `ui://` resources from MCP servers and rendered in the frontend's iframe sandbox (`McpAppHost.tsx`)
- **Unified entity design** — all artifacts use a single `Artifact` entity with one `to_dict()` method. `Collection` is an alias for `Artifact`. Container artifacts are distinguished by `content_type`.
- **Fractional indexing** — workspace artifact ordering uses lexicographic base-62 keys to avoid renumbering on reorder
- **Artifact reference model** — cross-artifact references are always a single `artifact_id` string (UUID); no embedded content, no workspace-scoped references
