# Agience Roadmap

This document tracks what's built, what's in progress, and what's coming next.

---

## Done

### Core platform

- **Artifact model** — every object (document, transcript, agent, transform, MCP server, collection) is a typed artifact with content, metadata, and JSON context. MIME content types drive rendering and dispatch.
- **Dual-database architecture** — PostgreSQL for ephemeral workspaces; ArangoDB for committed, versioned collections with graph relationships.
- **Workspace ↔ Collection boundary** — the commit is an explicit human act. Nothing is published silently.
- **Full artifact version history** — every committed artifact retains its full lineage in ArangoDB.
- **Fractional/lexicographic ordering** — artifacts have stable drag-reorder positions (base-62 order keys) with no renumbering.

### Knowledge curation

- **Semantic extraction** — transcripts and documents produce typed unit artifacts: `decision`, `constraint`, `action`, `claim`. Sources and evidence quotes are attached.
- **Commit Preview** — warns on decision/constraint artifacts with missing provenance before commit.
- **Commit flow** — promotes selected workspace artifacts into versioned collection entries.
- **Inline editing** — edit artifact title, description, and context in-place without a modal.
- **Card state actions** — new (delete), unmodified (remove/archive), modified (revert), archived (restore).

### Search

- **Hybrid search** — BM25 (lexical) + kNN (OpenAI embeddings) with RRF fusion, aperture filtering, and configurable field-boost presets.
- **Search query language** — `+required`, `-excluded`, `tag:`, `type:`, `collection:`, `~semantic` operators; per-token semantic modifier. See [`.docs/features/search-query-language.md`](.docs/features/search-query-language.md).
- **Workspace-scoped search** — full hybrid search within a workspace via OpenSearch.

### Auth & identity

- **Multi-provider OAuth2** — Google, Microsoft Entra, Auth0, custom OIDC, and username/password.
- **RS256 JWT tokens** — JWKS published at `/.well-known/jwks.json`; key rotation via `kid`.
- **`client_id` claim in all tokens** — OAuth `client_id` param for browser clients; API key name for MCP server/agent tokens. Tokens are traceable to their issuing client.
- **Refresh tokens** — 30-day refresh, 12-hour access.
- **Scoped API keys** — `resource|tool|prompt : mime : action [: anonymous]` scope format; stored hashed.
- **API key → JWT exchange** — `POST /api-keys/exchange` mints a short-lived JWT carrying scopes and `client_id` from the key name.
- **First-login provisioning** — inbox seed collection seeded into the user's workspace on first login.

### MCP

- **MCP server** — Streamable HTTP at `/mcp` (FastMCP); advertised via `/.well-known/mcp.json`.
- **9 tools** — `search`, `get_card`, `browse_collections`, `browse_workspaces`, `create_card`, `update_card`, `manage_card`, `extract_information`, `ask`. Streaming tools (`list_streams`, `transcribe`) are owned by Astra.
- **MCP client** — external MCP servers registered as `vnd.agience.mcp-server+json` artifacts; platform proxies calls via `mcp_service.py`.
- **ASGI auth middleware** — JWT or API key verified per-request; identity injected as context var.
- **VS Code integration** — works with the VS Code MCP extension. See [`.docs/mcp/`](.docs/mcp/).
- **Official-first policy** — Agience does not re-implement what vendor MCP servers (GitHub, filesystem, AWS) already provide.

### Agent architecture

- **Unified `POST /agents/invoke`** — single endpoint for LLM dispatch, named task agents, and Transform artifact execution.
- **Transform artifacts** — `vnd.agience.transform+json` artifacts define agent workflows; invoke by `transform_id`.
- **Chat artifact architecture** — "Ask anything" header creates a `vnd.agience.chat+json` artifact; auto-opens a floating `ChatWindow` card that runs an agentic loop (8-tool surface, max 8 iterations).
- **Function agents** — Python callables in `backend/agents/`; merged params + injected `workspace_id`/`cards`.

### File handling

- **Presigned S3 uploads** — direct browser-to-S3, no backend proxy. Real-time progress tracking.
- **Multi-file drag-and-drop** — cards created immediately; upload runs in background.
- **Small-file inline storage** — text files < 128 KB stored directly in artifact `content` field (optimal for LLM context).
- **CloudFront CDN delivery** — signed URLs (5-minute expiry) with long CDN cache (1 year) for private content.
- **Orphan cleanup** — S3 objects deleted when uncommitted upload artifacts are removed.

### Live streaming

- **SRS 5 ingest** — OBS stream key format `{source_artifact_id}:{api_key}`.
- **AWS Transcribe Streaming** — real-time transcript artifacts accumulate during session.
- **Stream source and session artifacts** — source artifacts committed to shared collections; transcript artifacts committed on stream end.

### UI

- **CardGrid** — flow-layout and free-position toggle with localStorage persistence. Ordered drag payload.
- **Card hover state transfer** — on delete, hover transfers to the next card without mouse movement.
- **Collection picker** — browse and select committed collections for references and commits.
- **Drag-and-drop reorder** — artifacts send ordered IDs via `PATCH /workspaces/{id}/order`.
- **Inbound webhooks** — external events create workspace artifacts. See [`.docs/features/inbound-webhooks.md`](.docs/features/inbound-webhooks.md).

### Agent servers (live tools)

| Server | Implemented tools |
|---|---|
| **Astra** | `ingest_file`, `list_streams` |
| **Jarvis** | `search`, `get_card`, `browse_collections`, `search_azure`, `index_to_azure`, `generate_meeting_insights` |
| **Verso** | `synthesize` |
| **Aria** | `format_response`, `present_card` |
| **Atlas** | `check_provenance`, `detect_conflicts`, `apply_contract` |
| **Nexus** | `send_message`, `get_messages`, `list_channels`, `exec_shell` |

---

## In Progress

- **Global cross-source search** — workspace-scoped search is complete; querying workspaces and collections in a single request is not yet wired. Backend needs query fan-out; frontend needs `searchGlobal()` updated to request both source types.
- [ ] **Transform artifact execution engine** — invoke-by-`transform_id` returns 501 today; full dispatch wiring is next.
- [ ] **Palette** — scaffolding and artifact type registered; execution engine not yet productized.

---

## Fast Follow

### Comms plane (Nexus + Jarvis loop)

The MVP demo loop requires an inbound → route → answer → outbound reply cycle on a real comms plane. Telegram is the target; Slack is out of scope for now.

- [ ] **Telegram gateway** — Nexus adapter: GUID webhook endpoint (no workspace ID in URL), bot token stored as user secret, inbound artifact creation.
- [ ] **Inbound → Jarvis routing** — Nexus routes inbound message to Jarvis grounded-answer run.
- [ ] **Outbound reply** — Nexus sends Telegram reply; receipt artifact links inbound → tool calls → outbound.
- [ ] **Receipt hardening** — every external side-effect records a durable receipt/provenance artifact. Receipts are required, not optional.

### Control plane (Timer + Inbox)

- [ ] **Timer artifacts** — `vnd.agience.timer+json` artifact type; schedule (one-shot / interval), enabled flag, target tool invocation spec, routing target.
- [ ] **Scheduler loop** — backend service evaluates timers on tick; fires actions through the same `/agents/invoke` pathway; emits receipts.
- [ ] **Inbox primitive** — `vnd.agience.inbox+json` artifact type with `new → triaged → resolved` state machine; links to evidence, drafts, and receipts.
- [ ] **Approval gating** — Inbox items with `requires_approval: true` block external sends until a human approves; approval fires the action and records a receipt.
- [ ] **Proactive triggers** — timers, inbound comms, and meeting events create Inbox items rather than auto-acting.

### Live meeting UI

- [ ] **Live meeting surface** — workspace UI surface that renders transcript updates and extracted unit artifacts in real time without manual refresh.
- [ ] **Live action list** — incremental action-unit artifacts appear as the meeting progresses.

### Search

- [ ] **Global cross-source search** — single query across workspaces + collections (fan-out + merge in backend, unified result set in frontend).

### Agent server stubs → implementations

These tools are scaffolded (return `"TODO: ..."`); they need real implementations:

| Server | Remaining stubs |
|---|---|
| **Astra** | `validate_input`, `normalize_card`, `deduplicate`, `classify_content`, `connect_source`, `sync_source`, `transcribe`, `collect_telemetry` |
| **Jarvis** | `ask`, `extract_information`, `research`, `cite_sources` |
| **Verso** | `run_workflow`, `chain_tasks`, `schedule_action`, `evaluate_output`, `submit_feedback` |
| **Aria** | `render_visualization`, `adapt_tone`, `narrate` |
| **Atlas** | `suggest_merge`, `traverse_graph`, `attribute_source`, `check_coherence`, `request_approval` |
| **Nexus** | `create_webhook`, `health_check`, `list_connections`, `register_endpoint`, `route_request`, `tunnel`, `proxy_tool` |
| **Seraph** | All tools (`audit_access`, `check_permissions`, `grant_access`, `revoke_access`, `rotate_api_key`, `verify_token`, `list_audit_events`, `sign_card`, `enforce_policy`, `list_policies`, `check_compliance`) |
| **Ophan** | All tools (payments, ledger, reconciliation, invoicing, market data, portfolio) |

### Platform

- [ ] **Content-type handler isolation** — remote viewers as web components; content-type apps loaded without bundling into core.
- [ ] **Desktop relay host** — signed installer; connects local tools to hosted or self-hosted Agience.
- [ ] **Browser extension relay** — Chrome / Firefox extension; same local-tool relay capability as the desktop host.
- [ ] **Contribution tracking** — per-artifact attribution; who contributed what and when.
- [ ] **Knowledge history browser** — full version lineage for every artifact; diff between any two versions.
- [ ] **Person artifact and identity** — public profile artifact per user; identity references across the graph.
- [ ] **Light-cone graph authorization** — collection-graph-aware access control propagation.
- [ ] **Validation and certification** — explicit validation mode; certification receipts for approved knowledge units.
- [ ] **Temporal knowledge state** — reconstruct workspace or collection state at any past point in time.
- [ ] **Matrix integration** — second comms-plane adapter for Nexus after Telegram is stable.
- [ ] **Cryptocurrency settlement** — on-chain value transfers via Ophan; wallet abstraction, signing, and human approval gating before execution.
- [ ] **Blockchain provenance signing** — cryptographic signing of committed artifacts against an on-chain anchor; provenance chain verifiable without trusting the platform.

