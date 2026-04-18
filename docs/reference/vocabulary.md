# Agience Vocabulary Reference

Status: **Reference**
Date: 2026-04-01

Canonical glossary for developers and operators. All terms are stable and enforced across
code, API, documentation, and MCP tooling. Terms marked with a code-usage note have a
direct mapping to identifiers in the codebase.

> **Design principle**: Humans look at Cards. Agents look at Artifacts.

---

## Core Data Model

**Information** — The abstract concept: a meaningful unit of knowledge described by the
Content / Context / Transform triangle (see Information Triangle below). Information is
never used as an API noun, database entity name, route parameter, or test fixture name.
It is the conceptual foundation from which all engineering nouns derive. Appears in
conceptual docs, the manifesto, and vision writing only.

**Artifact** — An addressable, stored, versioned, committable instance of Information.
The universal engineering noun across code, API, database, and documentation. An artifact
carries the three-part trinity: Content (payload), Context (metadata + provenance), and
a Transform reference (how it was produced). Everything in the platform is represented as
an artifact at the data layer.
Code usage: `Artifact`, `artifact_id`. `Collection` is an alias for `Artifact`.
API paths: `/artifacts`, `/artifacts/{id}`, `/artifacts/{id}/invoke`.
MCP tools: `get_artifact`, `create_artifact`, `update_artifact`, `manage_artifact`.
DB collection: unified `artifacts` (ArangoDB), with `collection_id` referencing the
container and `state` discriminating draft/committed/archived.
See also: Card (UI surface for the same data), Collection, Workspace.

**Content** — The raw payload inside an artifact, stored in `artifact.content`. Covers
human-facing text (notes, summaries, claims), media references (uploads, streams,
extracted text), and structured objects (JSON specs, transform definitions). Maps
directly to the standard MIME `content-type` field. Content is stored in S3 via
presigned upload; small text files may be stored inline in ArangoDB as a fallback.
Code usage: `artifact.content` field. See also: Context, Content type.

**Context** — A JSON payload attached to every artifact, stored in `artifact.context`.
Context positions an artifact in the platform ontology: it records type, semantic kind,
provenance, access boundaries, version lineage, and any handler-specific structured
metadata. Context is what makes artifacts classifiable, comparable, routable, and
reusable across operations. Core services treat context as opaque JSON — only the
handler that owns a content type may parse its internal structure.
Code usage: `artifact.context` field. Key sub-fields: `content_type`, `semantic.kind`,
`semantic.sources`, `semantic.evidence`, `transform`, `upload`.
See also: Content type, Receipt / Provenance.

**Transform** — One vertex of the Information Triangle. Describes *how* information is
produced, used, or transformed. Transform is always represented as a UUID reference to
a Transform artifact — never embedded inline in a parent artifact. This ensures
transforms are independently versioned, reusable, and committable. Conceptually,
a Transform is either an executable Operator (a callable surface that maps inputs to
outputs) or a composed Workflow (a graph of operator invocations).
Code usage: `artifact.context.transform` holds the UUID reference.
See also: Operator, Step, Information Triangle.

**Card** — A UI component that presents an artifact to a human. Cards exist only in the
frontend React layer — they are not a data entity name, API parameter, or database
identifier. A CardGrid lays out multiple artifacts as cards. A FloatingCardWindow opens
a single artifact for detailed viewing or editing.
Code usage: `<Card>`, `CardGrid`, `CardGridItem`, `CardBrowser`, `CardListItem`,
`FloatingCardWindow` (React components only — `frontend/src/components/`).
See also: Artifact (the underlying data entity).

**Collection** — A named, durable, versioned set of committed artifacts, stored in
ArangoDB. Collections provide immutable version history and are the platform's
canonical truth boundary. Use collections for stabilised specs, approved reports,
canonical evidence packs, and distributable templates. Access is controlled via Grants.
Code usage: `Collection` (alias for `Artifact`), `collection_id`.
A collection is a container artifact with `content_type = COLLECTION_CONTENT_TYPE`.
Children linked via `collection_artifacts` edge collection.
See also: Workspace (draft area), Commit (the promotion operation).

**Workspace** — A draft, high-churn staging area stored in ArangoDB. Workspaces have a
dual surface: humans interact with artifacts via Cards; agents interact with the same
data as Artifacts. Edits may overwrite prior versions — workspaces are not the
immutable history boundary. Artifacts are promoted from a workspace into a Collection
via a commit operation.
Code usage: `Collection` (alias for `Artifact`), `workspace`.
A workspace is a container artifact with `content_type = WORKSPACE_CONTENT_TYPE`.
API: `/artifacts` (same unified API). ArangoDB: `artifacts` collection.
See also: Collection, Artifact, Card.

**Receipt / Provenance** — A durable record of an execution, embedded in an output
artifact's context. Links inputs → tools invoked → outputs produced with enough detail
to audit and (ideally) reproduce. Three postures: Layer A (always-on: origin, timestamps,
actor, run IDs), Layer B (best-effort: source and evidence pointers), Layer C (validated
under policy with an explicit receipt). The output artifact *is* the audit record — no
separate run artifact is required.
Code usage: `artifact.context.semantic.sources`, `artifact.context.semantic.evidence`.
See also: Context, Tool call.

---

## Execution

**Information Triangle** — The three-component model that describes any unit of
Information: **Content** (what it is — the payload), **Context** (where it fits —
ontology, provenance, identity), and **Transform** (how it is produced or used —
operational semantics). Transform is the abstract concept; Operator is the concrete,
callable execution surface that realizes it. Any two components can predict the third. Completeness levels:
L1 Content-only (raw upload), L2 Content + Context (classified and attributable),
L3 all three (actionable and reproducible), L4 training/eval-ready (L3 plus strong
provenance and a deterministic evaluation contract or scoring rubric).

**Operator** — A concrete, callable execution surface — the artifact or entity that
performs a Transform. An Operator may be any scale: a single MCP tool call, a
multi-step composed sequence, an LLM invocation, or a human approval gate. Other
artifacts reference Operators by UUID; Operators are never embedded inline. A composed
Operator (a sequence of Steps) is still just an Operator — there is no separate
"Workflow" type at the data layer.
Code usage: `transform_id` field (legacy name for the Operator reference),
`application/vnd.agience.transform+json` MIME type.
See also: Transform, Step, Handler.

**Step** — A single operation within a composed Operator. A Step is one of: a Tool call
(MCP tool invocation), a Resource fetch (MCP resource read or import), an LLM transform
(prompted transform, ideally represented as a tool), or a Human action (approval, edit,
or labelling). Steps execute sequentially; DAG and parallel execution are future-state.
Code usage: `steps` array in Operator artifact context.
See also: Operator, Tool call.

**Tool call** — An explicit invocation of an MCP tool. Tool calls are the core unit of
work in Agience solutions. Inputs and outputs should be recorded (with provenance) on
the output artifact's context. Every tool call should produce traceable artifacts rather
than side-effecting state silently.
Code usage: MCP `tools/call` protocol. Execution flows through `POST /artifacts/{id}/invoke` via `artifacts_router.py` → `operation_dispatcher` → `agent_service.invoke()`.

**Event** — A signal that something occurred in the platform. Types include artifact
created/updated/deleted, upload completed, inbound message received, and commit events.
Events are the trigger mechanism for reactive automation via Handlers.
Code usage: event type strings such as `artifact_created`, `upload_completed`
(`card_created` is the legacy name — do not introduce new uses).

**Handler** — An Operator triggered by an Event. A Handler artifact declares which
Events it responds to (e.g. `on: [artifact_created]`) and which actions to run. Handlers
are the platform's reactive automation primitive. Scheduled triggers (cron-like) are
future-state via a scheduler MCP server.
Code usage: `workspace_event_handler.py`, Handler artifact context schema.
See also: Event, Operator, Trigger.

**Trigger** — An event that starts automation. Encompasses both event triggers (artifact
created/updated, inbound message received, commit events) and scheduled triggers
(cron-like, future-state via a scheduler MCP server). See Event and Handler for the
canonical treatment.

**Agent** — A manifest that binds an Operator to a Host (compute boundary) and an
Identity (who or what is running it). An Agent's knowledge should be represented as
artifacts or collections so it is inspectable and shareable. Recommended decomposition:
Identity + Allowed Tools/Resources + Knowledge/Memory + Allowed Hosts.
Code usage: `vnd.agience.agent+json` content type, `backend/agents/` (function-based
task agents in the Handler layer).
See also: Agency, Host.

**Agency** — A grouping of Agents, either organisationally or functionally. Provides a
unit of trust boundary and policy scope above the individual Agent level.
Code usage: `vnd.agience.agency+json` content type (skeleton).

**Routing** — Dispatching work to the right Tool, Operator, or Agent based on intent,
policy, and context. Typically implemented as MCP tool calls, with provenance recorded
on outputs. Not a separate service — routing logic lives in Operators and the Connection
layer.
See also: Connection, Operator.

**HITL gate (Human-in-the-loop)** — A governance boundary within an Operator or
workflow. LLM audits propose; humans approve stabilisation, waivers, and spec changes.
Represented as a Step of type Human action within a composed Operator.
See also: Step, Constraint.

**Constraint** — A rule that must hold, attached to or referenced by artifacts.
Deterministic constraints are repeatable checks (tests, linting, schema validation).
Non-deterministic constraints are interpretive audits (LLM drift, duplication, or
conflict detection). Typically enforced by Atlas (governance server).

---

## Platform Structure

**Content type** — A MIME type string that drives UI viewer selection and handler
dispatch. Standard MIME types (`text/*`, `image/*`, `application/json`, `application/pdf`,
etc.) are handled by platform-native renderers (Core). Agience-owned vendor types
(`application/vnd.agience.*`) are handled by first-party MCP servers. Third-party vendor
types are handled by the MCP server that defines them.
Code usage: `artifact.context.content_type`, `types/` directory for builtin skeletons,
`servers/*/ui/` for server-owned viewers and type definitions,
`frontend/src/registry/` for frontend resolution maps.

**Artifact taxonomy** — Four categories of artifact by structural role:
- **Container artifact** — represents a browsable universe; opening triggers a live query
  (e.g. Resources, Tools, Prompts, Collections).
- **Reference artifact** — a stable pointer to external truth with a minimal snapshot
  and a stable external ID; refreshable without overwriting authored annotations.
- **Knowledge artifact** — authored or derived knowledge units; must carry at minimum
  Layer A provenance.
- **Execution artifact** — workflow definitions (Transform artifacts) and execution
  provenance embedded in output artifact contexts.

**Host** — The computation boundary: the environment where computation actually runs
(local machine, container, remote runtime, or service boundary). A host provides
resources, permissions, execution context, and controls which tools may execute, which
secrets are available, and what network egress is permitted. A host may expose one or
more Servers.
Code usage: `vnd.agience.host+json` content type, `hosts/desktop/` directory
(desktop relay companion).
See also: Server, Agent.

**Server (MCP server)** — A tool and resource provider served via stdio or HTTP — the
transformation surface exposed from a Host. Servers expose Tools (callable functions)
and Resources (readable inputs) following the MCP protocol. Servers extend the platform
with domain-specific artifact types, handlers, and operators. The distinction: the Host
is *where* computation lives; the Server is *how* that computation is made callable.
First-party Agience servers: Aria (output), Sage (research), Atlas (governance),
Nexus (routing), Astra (ingestion), Verso (reasoning), Seraph (security), Ophan
(licensing).
Code usage: `servers/` directory, `vnd.agience.mcp-server+json` content type.
See also: Host, Tool, Resource.

**Tool** — A callable algorithm surface provided by an MCP server. Accepts input
arguments, produces output values, and may create or update artifacts as a side-effect.
Side-effect outputs must carry provenance and invocation links in their context. Never
build Agience-specific tools that duplicate what an official vendor MCP server provides.
Code usage: MCP `tools/list` / `tools/call` protocol, `backend/mcp_server/server.py`
for platform-native tools, `servers/*/server.py` for persona tools.

**Resource** — A readable thing: values, references, or sources. Resources arrive via
MCP resource catalogs, external indexes, or Agience collections and workspaces (Agience
acts as a resource provider via `agience://` URIs). When imported, resources become
reference artifacts or content artifacts.
Code usage: MCP `resources/list` / `resources/read` protocol. Viewer delivery uses
`ui://` resources. Platform resources served at `agience://collections/{id}` and
`agience://workspaces/{id}`.

**Knowledge** — A specialisation of Information. A knowledge artifact (or a collection
of artifacts) whose content and context describes tools, resources, connections, and
policies — i.e. Information *about how to compute and what to reference*. An Agent's
knowledge should be stored as artifacts so it is inspectable and shareable.

**Connection** — The pipeline and wiring layer for routing tool calls to servers and
hosts. A Connection projects credentials just-in-time, enforces policy constraints
(scopes, rate limits, audit), and selects the correct transport. Distinct from Transform,
which describes *what computation happens* — Connection describes *how invocations are
routed and credentialed*.
Code usage: OAuth connections and Authorizers, `features/oauth-connections-and-authorizers.md`.

**Communication plane** — A user-facing channel where an Agent can send or receive
messages (email, Slack, SMS, Telegram). Accessed through MCP tool calls to official
vendor servers; never via bespoke Core endpoints.

**Decision log** — A durable record of product or engineering decisions, typically
represented as artifacts linked to the specs or constraints they affect, with timestamp,
owner, rationale, and any waiver or exception references.

**Work item** — A planning and tracking unit (epic, feature, story, task, bug,
milestone). In MCP-first solutions, work items live in external systems (GitHub, Jira,
Linear) and are mirrored as reference artifacts for governance and provenance.

---

## Identity & Access

**Authority** — The root of trust for identities and policy claims. Issues and verifies
Person and Agent identities (or accepts an upstream IdP via OAuth/OIDC), mints JWT
tokens and claims, and optionally certifies validation receipts under policy.
Code usage: `AUTHORITY_ISSUER` config key, `vnd.agience.authority+json` content type,
`auth_service.py`, `key_manager.py`.

**Person** — A human principal. Authenticates via the Authority (Google, Microsoft,
Auth0, OIDC, or password). Owns workspaces and collections, or participates via shares
granted by another owner.
Code usage: `PersonEntity`, `person_id`, `person_service.py`.

**Authorizer** — Provider-specific OAuth flow logic. Handles the credential exchange
with a third-party identity provider and produces a Secret for storage.
Code usage: `complete_authorizer_oauth.py`, `features/oauth-connections-and-authorizers.md`.

**Secret** — Inbound credential material received from a third party (refresh tokens,
API secrets). Stored encrypted at rest using Fernet symmetric encryption in Core.
Delivered to MCP servers via JWE (RSA-OAEP-256 + AES-256-GCM) — the server's registered
RSA public key wraps the content encryption key so plaintext never transits the network.
Code usage: `SecretEntity`, `secrets_service.py`, `secrets_router.py`, `AgieceServerAuth.decrypt_jwe()`.

**Key** — An outbound credential issued to a client — typically a JWT access token
(RS256, 12-hour expiry) or a scoped API key for MCP servers and agents (no expiry,
revocable). Keys carry a Grant that scopes their permissions.
Code usage: `KeyEntity` (API key variant), `key_manager.py`, `api_keys_router.py`.
See also: Grant.

**Grant** — The permission bundle attached to a Key. Encodes least-privilege permissions
and resource constraints for the token bearer. Collections use Grants for access
control; always validate via `check_access()` (from `services/dependencies.py`).
Nine flags: can_create, can_read, can_update, can_delete, can_evict, can_add, can_share, can_invoke, can_admin.
Code usage: `GrantEntity`, `check_access()` in `services/dependencies.py`.
See also: Key.

**Server credential** — A credential used by an MCP server to authenticate to the
platform via the OAuth `client_credentials` grant. All first-party servers use
`PLATFORM_INTERNAL_SECRET` for a fast-path — no database lookup or `ServerCredential`
record required. Third-party servers use provisioned `ServerCredential` records.
All first-party servers share the consolidated `AgieceServerAuth` class from
`servers/_shared/agience_server_auth.py`.
Code usage: `ServerCredentialEntity`, `server_credentials_router.py`.

**Sharing / privacy boundary** — The access-control line that determines who can see
or act on an artifact. Enforced via Grants on Collections. Demonstrable through
share/access tooling and audit-friendly provenance receipts in artifact context.

---

## Cross-Reference Index

| If you see this term | Canonical term | Notes |
|---|---|---|
| `transform_id` | Operator | Legacy field name; holds an Operator artifact UUID |
| `vnd.agience.order+json` | `vnd.agience.transform+json` | Replaced — use `transform+json` |
| Card (data context) | Artifact | "Card" as a data entity is deprecated; use Artifact |
| `<Card>` / CardGrid / CardWindow | Card | UI component — correct usage, not deprecated |
| `workspace_cards` table | `workspace_artifacts` | Renamed |
| `collection_cards` collection | `collection_artifacts` | Renamed |
| `card_id` parameter | `artifact_id` | Renamed |
| `card_versions` collection | `artifact_versions` | Renamed |
| `cards_docs` / `cards_chunks` (OpenSearch) | `artifacts` | Consolidated into one canonical artifact index |
| Handler (layer architecture term) | Content-type handler | Architecture layer term — distinct from Handler (automation primitive above) |
| Order / Order card | Operator / Operator artifact | Legacy product terms superseded by Operator vocabulary |
| Agent card | Agent artifact | "Card" as data entity — use Artifact in data contexts |
