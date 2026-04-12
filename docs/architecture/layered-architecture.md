# Layered Architecture — Core / Handlers / Presentation

Status: **Reference**
Date: 2026-04-01

---

Agience is an OS-like platform. Like any OS, it has strict layer boundaries that prevent type-specific logic from leaking into the kernel, and prevent the shell from knowing about individual applications. This document defines those layers and the rules that enforce them.

---

## MCP Apps Protocol Alignment

Agience's layered architecture is fully aligned with and built on top of the **MCP Apps** extension (SEP-1865), the emerging standard for interactive UIs served by MCP servers.

### Protocol Summary

MCP Apps enables MCP servers to deliver interactive user interfaces to hosts:

- **`ui://` Resources**: Servers predeclare UI resources using the `ui://` URI scheme (MIME: `text/html;profile=mcp-app`)
- **Tool-UI Linkage**: Tools reference UI resources via `_meta.ui.resourceUri`
- **Bidirectional Communication**: UI iframes communicate with hosts using MCP JSON-RPC over `postMessage`
- **Security**: Mandatory iframe sandboxing, CSP enforcement, auditable communication
- **Extension Identifier**: `io.modelcontextprotocol/ui`

### Protocol Timeline

| Date | Event |
|------|-------|
| Mid-2025 | MCP-UI community project (@idosal) proves viability of MCP-served UIs |
| November 2025 | OpenAI Apps SDK launches, validating demand for rich UI in AI chat |
| November 21, 2025 | SEP-1865 PR filed — unifying MCP-UI + Apps SDK into one standard |
| January 26, 2026 | Stable spec `2026-01-26` released, SDK v1.0.0 |
| January 28, 2026 | SEP-1865 merged into MCP spec repo |
| March 2026 | SDK at v1.2.2, 38 contributors, adopted by Claude, ChatGPT, VS Code, Goose, Postman |

### How MCP Apps Maps to Agience Layers

```
  ┌───────────────────────────────────────────────────────────┐
  │  HOST (Agience Platform)                                  │
  │                                                           │
  │  ┌──────────────────────────────────────────────────────┐ │
  │  │  PRESENTATION (Shell / GUI)                          │ │
  │  │  Card chrome, CardGrid, window management, iframe    │ │
  │  sandbox host (McpAppHost)                            │ │
  │  │  Implements: Host side of MCP Apps protocol          │ │
  │  └──────────────────────────────────────────────────────┘ │
  │                       │                                   │
  │                   Registry                                │
  │                  (indirection)                             │
  │                       │                                   │
  │  ┌──────────────────────────────────────────────────────┐ │
  │  │  CORE (Kernel)                                       │ │
  │  │  Type-agnostic platform services, MCP infrastructure │ │
  │  │  Implements: MCP client, tool proxy, auth, storage   │ │
  │  └──────────────────────────────────────────────────────┘ │
  └───────────────────────────────────────────────────────────┘
                          │
                    MCP connection
                   (tools/call, resources/read,
                    ui:// resource delivery)
                          │
  ┌───────────────────────────────────────────────────────────┐
  │  SERVER (MCP Server — owns content-type handlers)         │
  │                                                           │
  │  ┌──────────────────────────────────────────────────────┐ │
  │  │  HANDLERS (Drivers / Apps)                           │ │
  │  │  Content-type-specific logic:                        │ │
  │  │  - UI: HTML Views served via ui:// resources         │ │
  │  │  - Backend: Tools (model + app visible)              │ │
  │  │  - Schema: context.ts, type.json, presentation.json  │ │
  │  │  Implements: Server side of MCP Apps protocol        │ │
  │  └──────────────────────────────────────────────────────┘ │
  └───────────────────────────────────────────────────────────┘
```

**The server MUST provide type handlers to the host.** Only the server knows how to handle its content. The platform (host) does NOT own type-specific viewers, context parsing, or tool surfaces.

| MCP Apps Concept | Agience Equivalent | Layer |
|------------------|--------------------|-------|
| `ui://` resource (HTML View) | Viewer for a content type | **Handler** (on server) |
| Sandboxed iframe | `McpAppHost` | **Presentation** (on host) |
| `ui/initialize` handshake | Host provides `hostContext` (theme, dimensions, locale) | **Presentation** (on host) |
| `_meta.ui.resourceUri` on tool | Maps tool results to a UI viewer | **Registry** (on host) |
| `tools/call` from View | View calls server for data (app-only or model+app) | **Core** (proxy) + **Handler** (implementation) |
| `resources/read` from View | View requests server resources | **Core** (proxy) + **Handler** (implementation) |
| `visibility: ["app"]` tools | Backend hooks for the View — hidden from model | **Handler** (on server) |
| `visibility: ["model", "app"]` tools | Standard MCP tools — model and View can both call | **Handler** (on server) |
| `ui/message` | View injects message into conversation | **Presentation** (on host) |
| `ui/update-model-context` | View pushes state for future model turns | **Presentation** (on host) |
| `ui/open-link` | View requests host to open URL | **Presentation** (on host) |
| CSP from `_meta.ui.csp` | Host enforces Content Security Policy on sandbox | **Presentation** (on host) |
| `hostContext.styles.variables` | Theming CSS variables passed to View | **Presentation** (on host) |
| Display modes (`inline`, `fullscreen`, `pip`) | Card window sizing/layout modes | **Presentation** (on host) |

### How Backend "Hooks" Work in MCP Apps

MCP Apps handles backend behavior through **tool visibility**, not through a separate hook mechanism:

1. **App-only tools** (`visibility: ["app"]`): Hidden from the model, callable only by the View. These are your "hooks" — form submissions, data refresh, state mutations, lifecycle actions.

2. **Dual-visibility tools** (`visibility: ["model", "app"]`): Both the model and the View can call them. Standard MCP tools that also power the UI.

3. **Resources** (`resources/read`): The View can read server resources directly through the host proxy.

Example — a "Save Settings" hook:
```json
{
  "name": "save_settings",
  "description": "Persist user settings",
  "inputSchema": { "type": "object", "properties": { "theme": { "type": "string" } } },
  "_meta": {
    "ui": {
      "resourceUri": "ui://my-server/settings-view",
      "visibility": ["app"]
    }
  }
}
```

The View calls `tools/call("save_settings", { theme: "dark" })` through the host. The model never sees this tool.

**Neither MCP nor MCP Apps defines artifact lifecycle hooks.** That is currently outside protocol scope. Artifact lifecycle events happen in the host platform (`create`, `commit`, `delete`, `archive`). When they occur, the host emits the event and may notify the authoritative type handler if that type's contract declares support for the event. `extract_text`, `summarize`, `generate_thumbnail`, etc. are not lifecycle hooks either — they are server capabilities invoked on demand. In both cases the wire operation is still just `tools/call`.

This is not necessarily a flaw in MCP Apps. MCP Apps standardizes UI delivery and host↔app communication. Artifact lifecycle semantics are host-domain semantics. Different hosts have different nouns and state transitions, so this belongs with the host unless multiple hosts converge on the same model and promote it into a formal MCP extension.

### Lifecycle Handler Registration

Because MCP does not define lifecycle hooks, the host needs a registration convention if it wants servers to participate in artifact lifecycle events.

The server should **not** need to know about Agience specifically. But it **can** know about artifact events and capability contracts if those are part of the shared handler contract.

That means registration should be **contract-based**, not ad hoc host-side mapping and not Agience-specific tool metadata.

Handler contract shape (declared in `behaviors.json` alongside the type definition):

```json
{
  "mime": "application/vnd.acme.invoice+json",
  "events": {
    "create": { "tool": "on_create" },
    "commit": { "tool": "on_commit" },
    "delete": { "tool": "on_delete" }
  },
  "capabilities": {
    "extract_text": { "tool": "extract_text" },
    "summarize": { "tool": "summarize" }
  }
}
```

The host does **not** hand-author per-server tool mappings. The server declares the contract for the types it owns; the host reads that contract and invokes the declared tool names through MCP.

Agience builds an active registry from:

1. discovered servers,
2. available type definitions,
3. type-local handler contracts,
4. trust and precedence policy.

When an artifact event occurs, the platform:

1. resolves the artifact MIME,
2. resolves the owner for that MIME,
3. reads the declared contract for that type,
4. looks up the declared tool for the event or capability,
5. invokes that tool with `tools/call`.

This keeps the relationship contract-driven: the host knows the event vocabulary, the server knows the tools it exposes for the types it owns, and neither side hardcodes the other's implementation details.

If the MCP ecosystem later standardizes host-managed artifact events, this contract can be aligned to the official extension mechanism.

### Artifact Event Emission Model

Agience emits artifact events. Type handlers do not emit them and do not own the event lifecycle. They only declare whether they want to be notified when the host emits a given event for a type they own.

OS analogy:

- The **host** is the kernel. It owns lifecycle state transitions.
- The **type handler** is the driver or application plugin. It can be notified after the kernel performs a state transition.
- The handler does not decide whether the commit happened. The host does.

Best-practice runtime rules:

1. **Emit after the state transition succeeds.** `commit` notification happens after commit succeeds, not before.
2. **Notify the authoritative handler for vendor types.** If Agience says "commit this artifact", the authoritative type handler for that artifact should be notified if the contract declares `commit`.
3. **Use idempotent handler tools.** Event notifications may be retried; handlers must tolerate duplicate delivery.
4. **Keep lifecycle notifications side-effect-safe.** A handler can react, enrich, index, sync, or validate downstream state, but it does not redefine the host's lifecycle semantics.
5. **Do not let notification failure corrupt host state.** Artifact commit/delete/archive success is determined by the host transaction, not by whether the notification tool succeeds.
6. **Use synchronous calls only when the host needs a result.** Capabilities like `extract_text` are request/response. Lifecycle notifications are usually fire-and-forget or background work.

This is the correct platform OS shape: the kernel emits events; handlers are notified according to a declared contract.

### Type Ownership and Handler Providers

Type ownership and handler provider selection are related, but they are not identical.

**Ownership** answers: who is authoritative for this MIME?

- A third-party vendor type is owned by the server that defines it.
- `application/vnd.agience.*` is always owned by Agience.
- Standard MIME types are not vendor-owned by third-party servers; Agience provides the default host implementation.

**Provider selection** answers: which implementation do we use at runtime?

- For vendor types, use the authoritative owner.
- For standard MIME types, use the builtin default unless policy selects a trusted alternate provider.

This is why standard MIME support can work like operating-system file associations without weakening ownership rules for vendor types.

### Type Ownership and Conflict Resolution

Type ownership is resolved by active registry rules, not by workspace lookup.

- A type defined by a server is owned by that server.
- A first-party Agience platform type is owned by Agience.
- Standard MIME types (`text/plain`, `text/markdown`, `application/json`, `image/*`, etc.) have platform-native default handlers, but servers may also provide alternate handlers for them.

Servers may be added or removed, and multiple servers may declare the same MIME. The platform therefore needs deterministic resolution rules:

1. **`application/vnd.agience.*` is always Agience-authoritative.**
2. **A third-party vendor MIME resolves to exactly one authoritative owner.**
3. **Standard MIME types may have one default plus multiple alternates.**
4. **Conflict without explicit precedence is an error.**
5. **Removal is dynamic** — when a server disappears, its owned types and alternate providers disappear from the active registry.

This resolution belongs in the type registry, not in workspaces.

### Standard MIME Handler Policy

Standard MIME types behave like operating-system file associations:

- Agience ships a default builtin handler for common standard MIME types.
- Trusted servers may register alternate handlers for those same standard MIME types.
- The registry stores one default platform handler plus zero or more alternate providers.
- Selection policy for standard MIME handlers may choose:
  - the platform default,
  - an explicitly selected trusted server handler,
  - or a deterministic configured preference.

What does **not** happen:

- Third-party servers do not take ownership of `application/vnd.agience.*`.
- Untrusted servers do not silently replace builtin handlers.
- Conflicting third-party claims do not auto-resolve by accident.

### Platform-Owned Types

Platform-owned types live under Agience's vendor namespace: `application/vnd.agience.*`.

- Their canonical type definitions live in the platform repository under `types/application/vnd.agience.*`.
- Their handlers still do **not** belong in Core just because they are first-party.
- If a `vnd.agience.*` type has a custom viewer or backend behavior, that handler is provided by an Agience-owned MCP server (or Agience's local MCP surface), not by hardcoded Core or Presentation logic.
- The only things that stay in-platform as direct builtins are platform-native standard MIME renderers and simple builtin capabilities for those standard MIME types.

This gives Agience two different ownership modes:

1. **Authoritative vendor types** — `application/vnd.agience.*`, always owned by Agience.
2. **Default standard MIME handlers** — builtin by default, but extensible through trusted server-provided alternate handlers.

---

## Three-Layer Model

```
  ┌──────────────────────────────────────────────────┐
  │  PRESENTATION (Shell / GUI)                      │
  │  Generic card chrome, layout, window management  │
  │  MCP Apps host: sandbox, JSON-RPC bridge, theme  │
  │  Depends on: Core contracts only                 │
  └──────────────────────────────────────────────────┘
                        │
                    Registry
                   (indirection)
                        │
  ┌──────────────────────────────────────────────────┐
  │  HANDLERS (Drivers / Apps)                       │
  │  Content-type-specific logic, viewers, tools     │
  │  Live on SERVERS, not the platform               │
  │  Delivered via ui:// resources (MCP Apps)         │
  │  Depends on: Core contracts only                 │
  └──────────────────────────────────────────────────┘
                        │
  ┌──────────────────────────────────────────────────┐
  │  CORE (Kernel)                                   │
  │  Type-agnostic platform services                 │
  │  MCP client infrastructure, tool proxy           │
  │  Depends on: nothing                             │
  └──────────────────────────────────────────────────┘
```

**Dependency rule:** Core depends on nothing. Handlers depend on Core contracts (via MCP protocol). Presentation depends on Core. Handlers and Presentation NEVER depend on each other directly — Presentation resolves Handlers through the Registry (an indirection layer owned by Core) and renders Views in sandboxed iframes per the MCP Apps protocol.

| Layer | OS Analogy | MCP Apps Role | Owns | Does NOT Own |
|-------|-----------|---------------|------|-------------|
| **Core** | Kernel, syscalls, VFS | MCP client, tool proxy, resource proxy | Artifact CRUD, workspace/collection lifecycle, auth, search, storage, agent dispatch infrastructure, MCP infrastructure, type resolution, registry | System prompts, tool surfaces, MIME constants, context schema parsing |
| **Handlers** | Device drivers, applications | MCP server: `ui://` resources, tools, resource providers | Viewers (HTML Views), context parsing, tool surfaces, system prompts, extraction logic, app-only tools | Window management, card chrome, layout, DB adapters, auth |
| **Presentation** | Window manager, desktop shell | MCP Apps host: sandbox, bridge, theming, display modes | Card frames, floating windows, grid/list layout, navigation, action dispatch, iframe sandbox, host context (theme/dimensions/locale), search panel | Type-specific rendering, context parsing, handler imports |

---

## Design Principles

### P1 — Type Blindness
Core services MUST NOT contain content-type-specific logic. If a function references a specific MIME type string, it does not belong in Core.

### P2 — Handler Owns Its Schema
Only the handler for content type X may understand the internal structure of X's `artifact.context`. Core passes context as an opaque JSON string or dict — it never reaches inside.

### P3 — No Content-Type Constants in Core
Core MUST NOT define constants like `MCP_SERVER_CONTENT_TYPE = "application/vnd.agience.mcp-server+json"`. MIME strings are handler-internal identifiers.

### P4 — Generic Core API Surface
Core endpoints serve all content types uniformly. If an endpoint is used by exactly one content type, it should be routed through a handler dispatch mechanism, not hardcoded in a core router.

### P5 — Handler-to-Core Contract
Handlers call core services via their public API. Handlers MUST NOT call database adapters directly, access other handlers' internals, or bypass service-layer orchestration.

### P6 — Presentation is Type-Oblivious
Presentation components MUST NOT branch on `contentType.id`, `contentType.mime`, or any type-specific identifier. Presentation resolves types through the registry and delegates all type-specific rendering to handler viewers.

### P7 — Registry as Indirection
All type-specific wiring (viewer component, icon, provider, actions) flows through the registry (`content-types.ts`, `viewer-map.ts`, `icon-map.ts`). Presentation reads from the registry — it never imports handler code directly.

### P8 — Action Dispatch, Not Action Handling
Presentation dispatches generic action IDs (`open`, `delete`, `archive`). If a content type needs custom actions, it declares them in `presentation.json` with unique IDs. Presentation renders them generically; the handler provides the implementation.

### P9 — Lifecycle Events Are Platform Events; Capabilities Are Server Tools
Artifact lifecycle events (create, commit, delete, archive) are platform events, not protocol features. A type may declare, as part of its handler contract, which of those host-emitted events its handler wants to receive. When the platform emits an event, it calls the owning server through MCP if the contract declares a handler for that event. On-demand behavior such as `extract_text`, `summarize`, and `generate_thumbnail` is exposed as normal server tools. In both cases the wire operation is `tools/call`. No type-specific endpoints in Core routers.

### P10 — MCP Apps Protocol as the Bridge
Viewer components (HTML Views served by MCP servers) communicate with the host exclusively through the MCP Apps JSON-RPC `postMessage` protocol. Views call `tools/call` and `resources/read` for server data. The host provides `hostContext` (theme, dimensions, locale, display mode) via `ui/initialize`. Views MUST NOT import from platform React contexts, modules, or internal APIs — they run in sandboxed iframes with no direct access to the host runtime. No exceptions.

### P11 — Server Owns Its Type Handlers
Only the owner of a content type provides its custom viewer and backend tools. For `application/vnd.agience.*`, Agience is always authoritative. For third-party vendor types, the defining MCP server is authoritative. The platform does not compile, bundle, or statically import type-specific viewer code. Viewers are served at runtime as `ui://` resources. The host renders them in sandboxed iframes. This is the MCP Apps pattern, and it is non-negotiable for `vnd.*` types.

### P12 — Platform-Native MIME Renderers Are Not Handlers
The platform MAY provide generic default renderers for standard MIME types (text/plain, text/markdown, application/json, image/*, audio/*, video/*, application/pdf). These are built-in platform capabilities, not vendor-owned content-type handlers. They don't use `ui://` resources — they render directly in Presentation. Trusted servers may register alternate handlers for those same standard MIME types, analogous to choosing Notepad++ instead of Notepad, but the builtin remains the default unless explicit policy selects otherwise.

---

## Decision Tests

Before placing any code, ask these questions:

| # | Question | If YES → |
|---|----------|----------|
| **D1** | Would this code need to change if a new content type is added? | **Wrong layer** — move to Handler or Registry |
| **D2** | Does this code reference a specific MIME type string? | If in Core or Presentation → **violation** |
| **D3** | Does this code parse specific fields inside `artifact.context`? | Must be in a **Handler's** `context.ts` or equivalent |
| **D4** | Does this backend endpoint serve exactly one content type? | Should be **handler-dispatched**, not a core router endpoint |
| **D5** | Does this React component import from a `content-types/` package? | Must be in the **Registry** layer or another handler — never Presentation |
| **D6** | Does this service have a hardcoded system prompt or tool list? | Type-specific logic — belongs in a **Handler** |
| **D7** | Would removing this content type require editing this file? | This file has a **type dependency** that violates its layer |
| **D8** | Does this component check `contentType.id === 'something'`? | **Presentation violation** — use registry-driven dispatch |

---

## File-to-Layer Mapping

### Backend

| Directory / File | Layer | Notes |
|---|---|---|
| `backend/core/` | **Core** | Config, dependencies, embeddings, key manager |
| `backend/db/` | **Core** | Database adapters (arango, arango_identity, opensearch) |
| `backend/entities/` | **Core** | Entity models (type-agnostic) |
| `backend/schemas/` | **Core** | DB schema initialization |
| `backend/search/` | **Core** | Search infrastructure |
| `backend/routers/` | **Core** | API route handlers (must stay type-agnostic) |
| `backend/api/` | **Core** | Domain API modules (called by routers) |
| `backend/mcp_server/` | **Core** | MCP server tool surface |
| `backend/mcp_client/` | **Core** | MCP client infrastructure |
| `backend/services/workspace_service.py` | **Core** | Artifact/workspace CRUD (strip type-specific functions) |
| `backend/services/collection_service.py` | **Core** | Collection CRUD and commit |
| `backend/services/content_service.py` | **Core** | S3 storage infrastructure |
| `backend/services/auth_service.py` | **Core** | JWT and auth |
| `backend/services/secrets_service.py` | **Core** | Credential storage |
| `backend/services/person_service.py` | **Core** | User lifecycle |
| `backend/services/types_service.py` | **Core** | Type resolution from `types/` directory |
| `backend/services/llm_service.py` | **Core** | LLM config resolution |
| `backend/services/agent_service.py` | **Core** | Agent invocation dispatch |
| `backend/services/openai_helpers.py` | **Core** | OpenAI API abstraction |
| `backend/services/seed_content_service.py` | **Core** | First-login provisioning |
| `backend/agents/` | **Handler** | Function-based task agents |
| `types/` (root directory) | **Handler** | Canonical type definitions. Includes first-party Agience `vnd.agience.*` types and mirrored server-owned types for registry resolution. |
| `backend/services/ingest_runner_service.py` | **Handler** | MIME classification logic — belongs in type definitions, not Core services |
| `backend/services/mcp_service.py` | **Mixed** | Infrastructure belongs in Core; `_artifact_to_mcp_config` (artifact parsing) belongs in Handler |

### Frontend

| Directory / File | Layer | Notes |
|---|---|---|
| `frontend/src/api/` | **Core** | Typed API client layer |
| `frontend/src/auth/` | **Core** | AuthProvider, OAuth flow |
| `frontend/src/context/workspace/` | **Core** | WorkspaceProvider (type-agnostic state) |
| `frontend/src/context/auth/` | **Core** | Auth context |
| `frontend/src/registry/` | **Core** | Type registry (viewer-map, icon-map, content-types) |
| `frontend/src/isolation/` | **Core** | MCP Apps host infrastructure — `McpAppHost.tsx` renders server-owned `ui://` resources in sandboxed iframes with MCP Apps JSON-RPC `postMessage` protocol. |
| `frontend/src/hooks/` | **Core** | Custom React hooks |
| `frontend/src/lib/` | **Core** | Utility libraries |
| `frontend/src/utils/` | **Core** | Utility functions |
| `frontend/src/types/` | **Core** | Global TypeScript types |
| `frontend/src/config/` | **Core** | Feature flags, app config |
| `frontend/src/constants/` | **Core** | Application-wide constants |
| `frontend/src/content-types/` | **Partial** | Contains platform-native MIME renderers (json, pdf, text, markdown, image, audio, video) which are legitimate per P12. Two empty `vnd.*` skeleton directories (`agency`, `agent`) remain. No new `vnd.*` entries may be added here. |
| `frontend/src/content-types/_shared/` | **Core** | Shared MIME renderer utilities for platform-native viewers. |
| `frontend/src/context/palette/` | **Presentation** | Type-agnostic palette UI state management, not a handler provider pattern. |
| `frontend/src/components/main/` | **Presentation** | MainLayout, MainHeader, MainFooter |
| `frontend/src/components/windows/` | **Presentation** | FloatingCardWindow (card chrome + window management) |
| `frontend/src/components/common/` | **Presentation** | CardGrid, CardGridItem, CardContextItems |
| `frontend/src/components/browser/` | **Presentation** | Artifact browser |
| `frontend/src/components/containers/` | **Presentation** | ContainerCardViewer |
| `frontend/src/components/search/` | **Presentation** | SearchPanel |
| `frontend/src/components/command-palette/` | **Presentation** | CommandPalette |
| `frontend/src/components/layout/` | **Presentation** | Layout components |
| `frontend/src/pages/` | **Presentation** | Top-level page components |
| `frontend/src/routes/` | **Presentation** | Route definitions |

---

## Handler Contract

A well-formed content-type handler lives on an **MCP server** (not the platform) and has three parts: a type definition, a View (UI), and backend tools. The handler communicates with the host exclusively through the MCP Apps protocol.

### Type Definition (required)

Location: `types/<category>/<subtype>/` (in the server's codebase, mirrored to platform `types/` for resolution)

| File | Required | Purpose |
|---|---|---|
| `type.json` | **Yes** | Identity: MIME, version, extensions, `inherits[]`, description |
| `presentation.json` | **Yes** | Display: label, icon key, color, badge, modes, states, viewer key, `creatable`, `actions[]` |
| `schema.json` | No | JSON Schema for `artifact.context` validation |
| `ui.json` | No | Property editor layout hints |
| `preview.json` | No | Preview/thumbnail rendering hints |
| `behaviors.json` | No | Optional type-local event contract for artifact lifecycle behavior (`create`, `commit`, `delete`, `archive`) |
| `handlers/*.json` | No | Optional type-local capability contract (`extract_text`, `summarize`, `generate_thumbnail`) |

### View — MCP App (required if type has a custom viewer)

Location: Server's codebase (e.g., `agience-server-template/frontend/content-types/<mime>/`)

The View is an HTML document served by the MCP server as a `ui://` resource. It renders inside a sandboxed iframe on the host. The View communicates with the host and server via MCP JSON-RPC over `postMessage`.

**View capabilities (MCP Apps protocol):**

| Capability | JSON-RPC Method | Direction | Purpose |
|---|---|---|---|
| Receive tool input | `ui/notifications/tool-input` | Host → View | Initial tool call arguments |
| Receive tool result | `ui/notifications/tool-result` | Host → View | Tool execution result (text + structuredContent) |
| Call server tools | `tools/call` | View → Host → Server | Data operations, form submissions, refresh |
| Read server resources | `resources/read` | View → Host → Server | Fetch server resources |
| Open external URL | `ui/open-link` | View → Host | Navigate to external page |
| Send chat message | `ui/message` | View → Host | Inject message into conversation |
| Update model context | `ui/update-model-context` | View → Host | Push state for future model turns |
| Request display mode | `ui/request-display-mode` | View → Host | Switch between inline/fullscreen/pip |
| Size changes | `ui/notifications/size-changed` | View → Host | Report content size for flexible containers |
| Host context updates | `ui/notifications/host-context-changed` | Host → View | Theme, display mode, dimension changes |
| Teardown | `ui/resource-teardown` | Host → View | Clean shutdown before iframe removal |
| Logging | `notifications/message` | View → Host | Log messages to host console |

**View initialization lifecycle:**

1. Host creates sandboxed iframe, loads `ui://` resource HTML
2. View sends `ui/initialize` with `appCapabilities` (supported display modes, tool support)
3. Host responds with `hostContext` (theme, styles, dimensions, locale, platform, tool info)
4. Host sends `ui/notifications/tool-input` with tool call arguments
5. Host sends `ui/notifications/tool-result` with tool execution result
6. Interactive phase begins — View can call tools, read resources, send messages

**View theming:**

The host provides CSS variables via `hostContext.styles.variables` (standardized by MCP Apps):
- Colors: `--color-background-primary`, `--color-text-primary`, `--color-border-primary`, etc.
- Typography: `--font-sans`, `--font-mono`, `--font-weight-*`, `--font-text-*-size`, etc.
- Borders: `--border-radius-*`, `--border-width-regular`
- Shadows: `--shadow-hairline`, `--shadow-sm`, `--shadow-md`, `--shadow-lg`
- Custom fonts via `hostContext.styles.css.fonts`

Views declare fallback values in `:root` for hosts that omit styles.

### Backend Tools (server-side handler behavior)

Backend behavior is exposed as MCP tools with appropriate visibility:

| Visibility | Who Can Call | Use Case |
|---|---|---|
| `["model", "app"]` (default) | Agent + View | Standard tools — model can invoke, View can also call |
| `["model"]` | Agent only | Model-facing tools hidden from View |
| `["app"]` | View only | Backend hooks — form submissions, refresh, mutations, lifecycle actions. Hidden from model. |

Example — a content-type handler's tool surface:
```json
[
  {
    "name": "get_server_status",
    "description": "Get current server status",
    "_meta": { "ui": { "resourceUri": "ui://my-server/status-view", "visibility": ["model", "app"] } }
  },
  {
    "name": "restart_service",
    "description": "Restart a managed service",
    "_meta": { "ui": { "resourceUri": "ui://my-server/status-view", "visibility": ["app"] } }
  }
]
```

The first tool is visible to the model and the View. The second is app-only — the View can call it (e.g., from a "Restart" button) but it's hidden from the model's tool list.

### Server-Side Resource Declaration

The server registers `ui://` resources at connection time:

```json
{
  "uri": "ui://my-server/settings-view",
  "name": "Settings",
  "description": "Interactive settings editor",
  "mimeType": "text/html;profile=mcp-app",
  "_meta": {
    "ui": {
      "csp": {
        "connectDomains": ["https://api.example.com"],
        "resourceDomains": ["https://cdn.example.com"]
      },
      "prefersBorder": true
    }
  }
}
```

The host prefetches resources during connection setup for performance and security review.

### Platform Lifecycle Events and Server Capabilities (H1 mechanism)

Orthogonal to the MCP Apps protocol, the platform needs to react to artifact lifecycle events and invoke server capabilities. MCP does not define artifact lifecycle events. Agience does.

**Lifecycle events** — event-driven. The platform observes an artifact event and may notify the owning server:

- `create`
- `commit`
- `delete`
- `archive`

**Server capabilities** — on-demand. The platform calls these when it needs something done:

- `extract_text`
- `summarize`
- `generate_thumbnail`

The runtime mechanism is the same in both cases: `tools/call` on the owning server. The difference is only what triggered the call.

Ownership is resolved from the active type registry. If a MIME is owned by a server, lifecycle notifications and capability calls go to that server. If a MIME is platform-native, the platform handles it directly with builtin logic.

### Host-Side Registration

For `ui://` resources served by MCP servers, the host:
1. Discovers tools with `_meta.ui.resourceUri` during MCP connection setup
2. Maps content type → `ui://` resource URI in the registry
3. When opening an artifact of that type, fetches the resource via `resources/read`
4. Renders the HTML in a sandboxed iframe via `McpAppHost`
5. Runs the MCP Apps communication protocol (initialize, tool-input, tool-result, etc.)

For platform-native MIME renderers (text, markdown, JSON, images, etc.), the host renders directly — no `ui://` resources or iframes needed.

---

## Further Reading

- [architecture/overview.md](overview.md) — high-level service, database, and communication patterns
- [architecture/content-types.md](content-types.md) — type definition format, `type.json`, `behaviors.json`, registry resolution
- [architecture/artifact-model.md](artifact-model.md) — artifact schema, state machine, reference model
- [mcp/server-development.md](../mcp/server-development.md) — build and register MCP servers for Agience

