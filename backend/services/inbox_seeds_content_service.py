"""
Inbox seeds content service -- auto-populates the seed sub-collections inside
the Agience Inbox Seeds collection.

Collections created/populated:
  agience-seeds-start-here         Onboarding guides for new users.
  agience-seeds-platform-artifacts Authority, Host, and links to Agience Servers / Tools / Agents.
  agience-seeds-all-servers        MCP server artifacts (populated by servers_content_service).
  agience-seeds-all-tools          Tool-catalog markdown document per server.
  agience-seeds-agents             Agent persona artifacts (linked from Resources collection).

The Agience Inbox Seeds parent collection (agience-inbox-seeds) is admin-only.
It links member collections so admins can see the nested structure.
Standard users are granted READ access to the sub-collections (see
bootstrap_types.USER_READABLE_SEED_SLUGS).

Idempotent -- safe on every restart and re-run via manage_seed.py --action populate.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from arango.database import StandardDatabase

from core.config import AGIENCE_PLATFORM_USER_ID
from db.arango import (
    add_artifact_to_collection as db_add_artifact_to_collection,
    create_artifact as db_create_artifact,
    create_collection as db_create_collection,
    get_artifact as db_get_artifact,
    get_current_in_collection as db_get_artifact_by_collection_and_root,
    get_collection_by_id as db_get_collection_by_id,
    get_edge as db_get_edge,
)
from services.collection_service import (
    db_get_latest_artifact_version_by_root_id,
)
from entities.collection import Collection as CollectionEntity
from entities.artifact import Artifact as ArtifactEntity
from services.bootstrap_types import (
    AUTHORITY_ARTIFACT_SLUG,
    HOST_ARTIFACT_SLUG,
    AGENT_ARTIFACT_SLUG_PREFIX,
    PLATFORM_AGENT_SLUGS,
    INBOX_SEEDS_COLLECTION_SLUG,
    START_HERE_COLLECTION_SLUG,
    PLATFORM_ARTIFACTS_COLLECTION_SLUG,
    ALL_SERVERS_COLLECTION_SLUG,
    ALL_TOOLS_COLLECTION_SLUG,
    AGENTS_COLLECTION_SLUG,
)
from services.platform_topology import get_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stable namespace for seed-content artifact root_ids (UUID5-based, no DB lookup needed)
# ---------------------------------------------------------------------------
_SEED_NS = uuid.UUID("c0bfeed4-0007-4000-a91e-ce5501234000")


def _seed_root_id(slug: str) -> str:
    """Derive a stable root_id from a seed-content slug. Deterministic across restarts."""
    return str(uuid.uuid5(_SEED_NS, slug))


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def ensure_all_seed_sub_collections(arango_db: StandardDatabase) -> None:
    """
    Ensure all four seed sub-collections exist and are populated. Called at startup
    and from manage_seed.py --action populate. Idempotent.
    """
    all_servers_id = _ensure_collection(
        arango_db,
        slug=ALL_SERVERS_COLLECTION_SLUG,
        name="Agience Servers",
        description="Platform server personas and their capabilities.",
    )
    all_tools_id = _ensure_collection(
        arango_db,
        slug=ALL_TOOLS_COLLECTION_SLUG,
        name="Agience Tools",
        description="Tool catalog for every platform server. Browse what each agent can do.",
    )
    agents_id = _ensure_collection(
        arango_db,
        slug=AGENTS_COLLECTION_SLUG,
        name="Agience Agents",
        description="Platform agent personas. Each agent has a domain specialty and delegates to MCP servers.",
    )
    start_here_id = _ensure_collection(
        arango_db,
        slug=START_HERE_COLLECTION_SLUG,
        name="Start Here",
        description="Onboarding guides for new users. Read these to get started with Agience.",
    )
    platform_artifacts_id = _ensure_collection(
        arango_db,
        slug=PLATFORM_ARTIFACTS_COLLECTION_SLUG,
        name="Platform Artifacts",
        description=(
            "Authority, Host, Servers, and Tools created when this instance was initialized. "
            "Browse these to understand what resources are available."
        ),
    )

    if all_servers_id:
        pass  # MCP server artifacts are populated by servers_content_service
    if all_tools_id:
        _populate_all_tools(arango_db, all_tools_id)
    if agents_id:
        _populate_agents(arango_db, agents_id)
    if start_here_id:
        _populate_start_here(arango_db, start_here_id)
    if platform_artifacts_id:
        _populate_platform_artifacts(arango_db, platform_artifacts_id, all_servers_id, all_tools_id, agents_id)

    # Link Start Here and Platform Artifacts directly into Inbox Seeds.
    _link_sub_collections_to_inbox_seeds(
        arango_db,
        start_here_id=start_here_id,
        platform_artifacts_id=platform_artifacts_id,
    )


# ---------------------------------------------------------------------------
# Collection creation helper
# ---------------------------------------------------------------------------

def _ensure_collection(
    arango_db: StandardDatabase,
    *,
    slug: str,
    name: str,
    description: str,
) -> Optional[str]:
    col_id = get_id(slug)
    existing = db_get_collection_by_id(arango_db, col_id)
    if existing:
        return existing.id

    try:
        now = datetime.now(timezone.utc).isoformat()
        from entities.collection import COLLECTION_CONTENT_TYPE
        col = CollectionEntity(
            id=col_id,
            name=name,
            description=description,
            created_by=AGIENCE_PLATFORM_USER_ID,
            content_type=COLLECTION_CONTENT_TYPE,
            state=CollectionEntity.STATE_COMMITTED,
            slug=slug,
            created_time=now,
            modified_time=now,
        )
        db_create_collection(arango_db, col)
        logger.info("Created seed sub-collection '%s' (id=%s)", name, col_id)
        return col_id
    except Exception:
        logger.exception("Failed to create seed sub-collection '%s' (id=%s)", name, col_id)
        return None


# ---------------------------------------------------------------------------
# Artifact creation / linking helpers
# ---------------------------------------------------------------------------

def _ensure_artifact_in_collection(
    arango_db: StandardDatabase,
    *,
    collection_id: str,
    root_id: str,
    slug: str,
    context: dict,
    content: str,
    content_type: Optional[str] = None,
) -> bool:
    """Create an artifact (if absent) and link it to the collection. Idempotent."""
    if db_get_artifact_by_collection_and_root(arango_db, collection_id, root_id):
        return True  # already linked

    existing = db_get_artifact(arango_db, root_id)
    if existing:
        try:
            db_add_artifact_to_collection(arango_db, collection_id, root_id, existing.id)
            return True
        except Exception:
            logger.exception("Failed linking existing artifact root %s to collection %s", root_id, collection_id)
            return False

    try:
        now = datetime.now(timezone.utc).isoformat()
        # id == root_id is the canonical first-version invariant. The edge
        # _to = "artifacts/{root_id}" must resolve to an existing document,
        # so _key (= id) must equal root_id for graph traversal to work.
        # content_type is a first-class artifact field — stored at top level,
        # not buried inside context.
        resolved_content_type = content_type or context.get("content_type")
        artifact = ArtifactEntity(
            id=root_id,
            root_id=root_id,
            collection_id=collection_id,
            state=ArtifactEntity.STATE_COMMITTED,
            context=json.dumps(context, separators=(",", ":"), ensure_ascii=False),
            content=content,
            content_type=resolved_content_type,
            created_by=AGIENCE_PLATFORM_USER_ID,
            created_time=now,
            slug=slug,
        )
        db_create_artifact(arango_db, artifact)
        db_add_artifact_to_collection(arango_db, collection_id, root_id)
        logger.info("Created seed artifact '%s' (root=%s)", slug, root_id)
        return True
    except Exception:
        logger.exception("Failed creating seed artifact '%s'", slug)
        return False


def _link_collection_as_member(
    arango_db: StandardDatabase,
    *,
    parent_collection_id: str,
    child_collection_id: str,
) -> bool:
    """
    Link a child collection artifact directly into a parent collection via an edge.
    Both IDs are artifact IDs — a collection IS an artifact. Idempotent.
    """
    if db_get_edge(arango_db, parent_collection_id, child_collection_id):
        return True  # already linked
    try:
        db_add_artifact_to_collection(arango_db, parent_collection_id, child_collection_id)
        logger.info("Linked collection %s into parent collection %s", child_collection_id, parent_collection_id)
        return True
    except Exception:
        logger.exception("Failed linking collection %s into parent %s", child_collection_id, parent_collection_id)
        return False


# ---------------------------------------------------------------------------
# Start Here content
# ---------------------------------------------------------------------------

_START_HERE_DOCS = [
    {
        "slug": "agience-doc-welcome",
        "title": "Welcome to Agience",
        "content": """\
# Welcome to Agience

Agience is a human-in-the-loop knowledge curation platform -- built like an OS for AI-powered work.

Think of it as a personal knowledge environment where **humans curate** and **agents assist**.

## The Core Idea

Information in Agience lives as **Artifacts** -- addressable, versioned, committable units of knowledge.
You interact with them as **Cards** in your workspace.

There are two surfaces:

- **Workspaces** -- Draft staging areas. Create, edit, and organise cards before committing.
- **Collections** -- Committed, versioned knowledge. Once you commit artifacts to a collection they're
  part of your permanent knowledge graph.

## How It Fits Together

```
Workspace (draft)  ->  Commit  ->  Collection (permanent)
     ↓                                  ↓
  Cards (UI)                       Artifacts (agents)
```

Agents see **Artifacts**. You see **Cards**. Same content, different surfaces.

## What's in Your Inbox

Your **Inbox** workspace is pre-populated with onboarding cards -- including this one.
Browse the other cards here to learn the basics before you start creating your own.

You can also explore the **Start Here** and **Platform Artifacts** collections in the left sidebar.

## Next Steps

1. Browse the other cards here to learn the basics.
2. Open a workspace and start creating cards.
3. Commit your work to a collection when you're ready.
4. Use search (⌘K / Ctrl+K) to find anything across all your knowledge.
""",
    },
    {
        "slug": "agience-doc-cards",
        "title": "Working with Cards",
        "content": """\
# Working with Cards

Cards are the primary way you interact with information in Agience. Every card represents an
**Artifact** -- a unit of knowledge with content and context.

## Card States

Every card has a state that tells you what's happening with it:

| State | Meaning |
|-------|---------|
| **Draft** | Work in progress, not yet committed to any collection |
| **Committed** | Committed to a collection -- versioned and searchable |
| **Archived** | No longer active, but preserved in history |

## Creating Cards

Click the **+** button in any workspace to create a new card. You can also:

- Drag and drop files to create cards from documents, audio, video, or images
- Use an agent to generate and create cards for you via chat

## Editing Cards

Click any card to open it. Edit the content directly -- changes are saved automatically to the workspace.

## Card Actions

When you hover over a card you'll see action buttons:

- **Delete** (draft cards) -- remove from workspace entirely
- **Commit** -- commit this card to a collection
- **Revert** -- undo changes since last commit
- **Archive** -- archive the card

## Content Types

Cards can hold many types of content: plain text, markdown, PDFs, images, audio, video, and
custom structured types for things like agents, servers, and transforms. The card viewer
automatically renders the correct format.
""",
    },
    {
        "slug": "agience-doc-collections",
        "title": "Collections and Commits",
        "content": """\
# Collections and Commits

Collections are your permanent knowledge store. When you commit a workspace artifact to a
collection it becomes **versioned, searchable, and shareable**.

## What Is a Collection?

A collection is a named, versioned set of committed artifacts -- like a repository or knowledge base
that holds the curated, trusted version of your work.

Collections have:

- A **name** and optional **description**
- An **owner** (you, or the platform)
- **Version history** -- every commit is recorded
- **Access control** -- share with others (read-only or read-write)

## The Commit Flow

1. **Draft** your cards in a workspace
2. **Review** your changes
3. **Commit** -- select cards and commit them to a collection

Once committed, that version of an artifact is locked. Future edits create a new version.

## Browsing Collections

Use the left sidebar to find collections:

- **My Collections** -- collections you own or have write access to
- **Shared With Me** -- collections others have granted you access to
- **Platform** -- system-managed collections (like this one)

## Search

Use the search bar (⌘K / Ctrl+K) to search across **all** your collections and workspaces at once.
Agience uses hybrid search (semantic + keyword) so natural-language queries work well.

## Sharing

From a collection's settings you can generate a share link with read (or read-write) access.
Shares use time-limited tokens backed by the grant system.
""",
    },
    {
        "slug": "agience-doc-agents",
        "title": "AI Agents and MCP",
        "content": """\
# AI Agents and MCP

Agience is built around **MCP (Model Context Protocol)** -- an open standard for connecting AI
models to tools and resources. Every agent in Agience is an MCP server.

## Platform Agents

Your instance includes eight built-in server personas:

| Agent | Specialty |
|-------|-----------|
| **Aria** | Output & presentation -- formats results for humans |
| **Astra** | Ingestion & indexing -- captures and prepares content |
| **Atlas** | Governance & coherence -- tracks provenance and contracts |
| **Sage** | Research & retrieval -- searches and synthesises knowledge |
| **Nexus** | Networking & routing -- connects services and channels |
| **Ophan** | Finance & licensing -- value transfers and entitlements |
| **Seraph** | Security & trust -- access control and cryptographic signing |
| **Verso** | Reasoning & workflows -- orchestrates multi-step tasks |

Browse **Platform Artifacts -> All Servers** for details on each server, and
**Platform Artifacts -> All Tools** for a complete tool catalog.

## Using Agents

You can invoke agents in three ways:

1. **Via Chat** -- type in the chat panel; Aria handles conversation and delegates to other agents
2. **Via the Command Palette** (⌘K) -- search for an agent action and run it directly
3. **Via Transforms** -- create an artifact with a `run` block that chains agent actions

## Adding External Servers

You can connect any MCP-compatible server to Agience by creating a card of type
`vnd.agience.mcp-server+json` in your workspace. Once registered, its tools become
available to agents.

## The MCP Advantage

Because agents communicate via MCP they're:

- **Composable** -- tools from different servers chain together
- **Discoverable** -- the platform can list all available tools at runtime
- **Secure** -- auth tokens are scoped per server and per workspace
""",
    },
]


def _populate_start_here(arango_db: StandardDatabase, collection_id: str) -> None:
    for doc in _START_HERE_DOCS:
        root_id = _seed_root_id(doc["slug"])
        _ensure_artifact_in_collection(
            arango_db,
            collection_id=collection_id,
            root_id=root_id,
            slug=doc["slug"],
            context={
                "content_type": "text/markdown",
                "title": doc["title"],
            },
            content=doc["content"],
        )


# ---------------------------------------------------------------------------
# Agience Agents content
# ---------------------------------------------------------------------------

def _populate_agents(arango_db: StandardDatabase, collection_id: str) -> None:
    """
    Link each platform agent artifact into the Agience Agents collection.
    The agent artifacts are owned by the Resources collection -- we just add them here too.
    Runs after resources_content_service has created the agent artifacts.
    """
    for agent_slug in PLATFORM_AGENT_SLUGS:
        artifact_slug = f"{AGENT_ARTIFACT_SLUG_PREFIX}{agent_slug}"
        try:
            root_id = get_id(artifact_slug)
        except RuntimeError:
            logger.warning(
                "Agent slug '%s' not resolved -- skipping from Agience Agents (will retry at next startup)",
                artifact_slug,
            )
            continue

        if db_get_artifact_by_collection_and_root(arango_db, collection_id, root_id):
            continue  # already linked

        existing = db_get_latest_artifact_version_by_root_id(arango_db, root_id)
        if existing:
            try:
                db_add_artifact_to_collection(arango_db, collection_id, root_id, existing.id)
                logger.info("Linked agent '%s' into Agience Agents collection", agent_slug)
            except Exception:
                logger.exception("Failed linking agent '%s' into Agience Agents collection", agent_slug)
        else:
            logger.warning(
                "No artifact version found for agent '%s' -- will be linked at next startup",
                agent_slug,
            )


# ---------------------------------------------------------------------------
# All Tools content
# ---------------------------------------------------------------------------

_SERVER_TOOLS: dict[str, list[tuple[str, str]]] = {
    "aria": [
        ("format_response", "Format content for human consumption with appropriate markup and layout."),
        ("render_visualization", "Create charts, diagrams, or visual representations of structured data."),
        ("adapt_tone", "Adjust language tone and style for a target audience."),
        ("present_card", "Present a card's content with appropriate formatting for human review."),
        ("narrate", "Generate a natural-language narrative from structured data."),
        ("run_chat_turn", "Run one agentic chat turn with tool calling. Returns the assistant reply and tool call log."),
    ],
    "astra": [
        ("ingest_file", "Create a workspace card from a file URL or raw text."),
        ("document_text_extract", "Extract text from a PDF artifact and create a derived text artifact."),
        ("validate_input", "Validate incoming data against a schema or content rules."),
        ("normalize_artifact", "Normalize card content -- standardize fields, clean formatting."),
        ("deduplicate", "Check for duplicate or near-duplicate content in a workspace."),
        ("classify_content", "Classify content by type, topic, or category using LLM analysis."),
        ("connect_source", "Register an external connector (Drive folder, inbox, Slack channel)."),
        ("sync_source", "Pull latest content from a registered connector into the workspace."),
        ("index_artifact", "Force re-index of a card into the search layer."),
        ("list_streams", "List active and recent live stream sessions."),
        ("collect_telemetry", "Collect and record system activity telemetry as workspace cards."),
        ("rotate_stream_key", "Generate or rotate the RTMP stream key for a stream source artifact."),
        ("get_stream_sessions", "Get active live sessions for a stream source artifact."),
    ],
    "atlas": [
        ("check_provenance", "Trace a card's version lineage and source attribution."),
        ("detect_conflicts", "Find cards with conflicting claims in a workspace or collection."),
        ("apply_contract", "Validate workspace cards against a contract card that defines rules."),
        ("suggest_merge", "Propose a merge of two divergent versions of a card."),
        ("traverse_graph", "Traverse relationship edges from a card in the knowledge graph."),
        ("attribute_source", "Link a card to its origin -- person, event, tool, or external document."),
        ("check_coherence", "Assess logical coherence across a set of cards."),
    ],
    "sage": [
        ("search", "Hybrid semantic + keyword search across workspaces and collections."),
        ("get_artifact", "Fetch a card by ID. Returns full card content and context."),
        ("browse_collections", "List committed collections accessible to the current user."),
        ("search_azure", "Search cards via an Azure AI Search index."),
        ("index_to_azure", "Project workspace cards into an Azure AI Search index."),
        ("research", "Multi-step retrieval + LLM synthesis constrained by evidence."),
        ("cite_sources", "Produce a provenance receipt citing the cards used in a synthesised answer."),
        ("ask", "Ask a question with optional card context -- search + synthesise an answer."),
        ("extract_information", "Extract structured fields from a card's content using a JSON schema."),
        ("generate_meeting_insights", "Derive summary, action items, and coaching from a transcript card."),
    ],
    "nexus": [
        ("send_email", "Send an email via the platform's configured email Authorizer (Gmail API)."),
        ("send_message", "Send a message via a registered channel adapter (Telegram, Slack, email)."),
        ("get_messages", "Poll a channel adapter for new messages since a given cursor."),
        ("list_channels", "List registered channel adapters for the current user."),
        ("create_webhook", "Register an inbound webhook and return its endpoint URL."),
        ("health_check", "Check the health and availability of a service endpoint."),
        ("list_connections", "List registered service connections and their current status."),
        ("register_endpoint", "Register a service endpoint for routing."),
        ("route_request", "Route a request to a registered service endpoint."),
        ("exec_shell", "Execute a shell command in a sandboxed directory."),
        ("proxy_tool", "Proxy an MCP tool call through a registered endpoint."),
    ],
    "ophan": [
        ("send_payment", "Initiate a value transfer (crypto or fiat)."),
        ("record_transaction", "Record an external transaction as a double-entry ledger card."),
        ("get_transaction", "Fetch a transaction by ID or on-chain hash."),
        ("list_transactions", "List transactions for an account over a date range."),
        ("fetch_statement", "Import a bank/exchange/payroll statement as workspace cards."),
        ("get_balance", "Get current balance for a wallet, bank account, or ledger account."),
        ("reconcile_account", "Match transactions against a statement and flag discrepancies."),
        ("create_invoice", "Generate an invoice card (accounts receivable)."),
        ("apply_payment", "Mark an invoice as paid and update the ledger."),
        ("run_report", "Produce a P&L, balance sheet, or cash-flow report card."),
        ("get_price", "Get current or historical price for a crypto or equity ticker."),
        ("get_market_data", "Fetch OHLCV data for a symbol over a date range."),
        ("track_wallet", "Monitor a blockchain wallet address for incoming activity."),
        ("get_portfolio", "Retrieve holdings from a connected brokerage or exchange."),
        ("calculate_pnl", "Calculate realised/unrealised P&L."),
        ("track_resource_usage", "Track resource consumption and resource allocation."),
        ("get_metrics", "Get system performance and efficiency metrics."),
        ("calculate_budget", "Calculate or project a budget for a workspace or project."),
        ("issue_license", "Create and sign a license artifact from entitlement inputs."),
        ("renew_license", "Extend or replace an existing license artifact."),
        ("revoke_license", "Revoke a license and record the compliance event."),
        ("review_installation", "Inspect installation and activation state."),
        ("record_usage_snapshot", "Ingest aggregate metering or usage snapshots."),
        ("run_licensing_report", "Produce licensing or entitlement report cards."),
    ],
    "seraph": [
        ("provide_access_token", "Exchange an Authorizer's stored refresh token for a fresh access token."),
        ("complete_authorizer_oauth", "Complete the OAuth authorization code exchange for an Authorizer artifact."),
        ("audit_access", "Query the access audit log for a resource, collection, or user."),
        ("check_permissions", "Check what a person or API key can access."),
        ("grant_access", "Grant access to a collection for a person or team."),
        ("revoke_access", "Revoke access to a collection."),
        ("rotate_api_key", "Rotate a scoped API key and return the new key details."),
        ("verify_token", "Verify a JWT or API key and return its decoded claims."),
        ("list_audit_events", "List recent security events (logins, grants, revocations, key usage)."),
        ("sign_card", "Create a tamper-evident cryptographic signature card for a card."),
        ("enforce_policy", "Evaluate a request or card against active system policies."),
        ("list_policies", "List active governance policies."),
        ("check_compliance", "Check compliance of a resource or workflow against governance rules."),
    ],
    "verso": [
        ("synthesize", "Synthesize information from multiple sources via LLM."),
        ("run_workflow", "Execute a multi-step workflow defined by a Transform artifact."),
        ("chain_tasks", "Chain multiple MCP tool calls sequentially into a pipeline."),
        ("schedule_action", "Schedule a deferred action for future execution."),
        ("evaluate_output", "Evaluate quality and accuracy of generated output."),
        ("submit_feedback", "Submit evaluation feedback for training improvement."),
    ],
}

_SERVER_DESCRIPTIONS: dict[str, tuple[str, str, str]] = {
    "aria": (
        "Aria", "Output & Presentation",
        "Formats and presents final responses for humans. Last-mile communication and visualization layer.",
    ),
    "astra": (
        "Astra", "Ingestion & Indexing",
        "Captures and prepares incoming information. First contact with external content.",
    ),
    "atlas": (
        "Atlas", "Governance & Coherence",
        "Provenance, attribution, and lineage. Tracks decisions, constraints, and policy coherence.",
    ),
    "sage": (
        "Sage", "Research & Retrieval",
        "Discovers sources, gathers evidence, and synthesizes evidence-backed answers.",
    ),
    "nexus": (
        "Nexus", "Networking & Routing",
        "Connects agents, services, and external systems via messaging, webhooks, and tunnels.",
    ),
    "ophan": (
        "Ophan", "Finance & Licensing",
        "Economic operations: value transfers, double-entry ledger, invoicing, licensing, and metrics.",
    ),
    "seraph": (
        "Seraph", "Security & Trust",
        "Access control, audit trails, identity verification, and cryptographic signing.",
    ),
    "verso": (
        "Verso", "Reasoning & Workflows",
        "Synthesis, multi-step orchestration, output evaluation, and training feedback.",
    ),
}


def _build_tool_catalog_content(server_slug: str) -> str:
    title, role, description = _SERVER_DESCRIPTIONS[server_slug]
    tools = _SERVER_TOOLS.get(server_slug, [])
    lines = [
        f"# {title} \u2014 Tool Catalog",
        "",
        f"**Role:** {role}",
        "",
        description,
        "",
        "## Available Tools",
        "",
    ]
    for tool_name, tool_desc in tools:
        lines.append(f"### `{tool_name}`")
        lines.append(tool_desc)
        lines.append("")
    lines.append("---")
    lines.append(f"*{len(tools)} tool{'s' if len(tools) != 1 else ''} available in {title}.*")
    return "\n".join(lines)


def _populate_all_tools(arango_db: StandardDatabase, collection_id: str) -> None:
    for server_slug in PLATFORM_AGENT_SLUGS:
        slug = f"agience-tools-{server_slug}"
        root_id = _seed_root_id(slug)
        title, _, _ = _SERVER_DESCRIPTIONS[server_slug]
        _ensure_artifact_in_collection(
            arango_db,
            collection_id=collection_id,
            root_id=root_id,
            slug=slug,
            context={
                "content_type": "text/markdown",
                "title": f"{title} Tools",
                "server": server_slug,
            },
            content=_build_tool_catalog_content(server_slug),
        )


# ---------------------------------------------------------------------------
# Platform Artifacts content
# ---------------------------------------------------------------------------

def _populate_platform_artifacts(
    arango_db: StandardDatabase,
    collection_id: str,
    all_servers_id: Optional[str],
    all_tools_id: Optional[str],
    agents_id: Optional[str],
) -> None:
    # Link Agience Servers, Agience Tools, and Agience Agents directly as members.
    if all_servers_id:
        _link_collection_as_member(
            arango_db,
            parent_collection_id=collection_id,
            child_collection_id=all_servers_id,
        )
    if all_tools_id:
        _link_collection_as_member(
            arango_db,
            parent_collection_id=collection_id,
            child_collection_id=all_tools_id,
        )
    if agents_id:
        _link_collection_as_member(
            arango_db,
            parent_collection_id=collection_id,
            child_collection_id=agents_id,
        )

    # Link Authority artifact
    try:
        authority_root_id = get_id(AUTHORITY_ARTIFACT_SLUG)
        if not db_get_artifact_by_collection_and_root(arango_db, collection_id, authority_root_id):
            existing = db_get_latest_artifact_version_by_root_id(arango_db, authority_root_id)
            if existing:
                db_add_artifact_to_collection(arango_db, collection_id, authority_root_id, existing.id)
                logger.info("Linked Authority artifact into Platform Artifacts collection")
            else:
                logger.warning("Authority artifact not yet created -- will link at next startup")
    except RuntimeError:
        logger.warning("Authority artifact root not resolved -- skipping")
    except Exception:
        logger.exception("Failed to link Authority artifact into Platform Artifacts")

    # Link Host artifact
    try:
        host_root_id = get_id(HOST_ARTIFACT_SLUG)
        if not db_get_artifact_by_collection_and_root(arango_db, collection_id, host_root_id):
            existing = db_get_latest_artifact_version_by_root_id(arango_db, host_root_id)
            if existing:
                db_add_artifact_to_collection(arango_db, collection_id, host_root_id, existing.id)
                logger.info("Linked Host artifact into Platform Artifacts collection")
            else:
                logger.warning("Host artifact not yet created -- will link at next startup")
    except RuntimeError:
        logger.warning("Host artifact root not resolved -- skipping")
    except Exception:
        logger.exception("Failed to link Host artifact into Platform Artifacts")


# ---------------------------------------------------------------------------
# Inbox Seeds parent -- member collection links (admin visibility)
# ---------------------------------------------------------------------------

def _link_sub_collections_to_inbox_seeds(
    arango_db: StandardDatabase,
    *,
    start_here_id: Optional[str],
    platform_artifacts_id: Optional[str],
) -> None:
    """
    Link Start Here and Platform Artifacts directly into Inbox Seeds as member collections.
    A collection IS an artifact; no proxy artifact is needed.
    """
    try:
        inbox_seeds_id = get_id(INBOX_SEEDS_COLLECTION_SLUG)
    except RuntimeError:
        logger.warning("Inbox Seeds collection ID not registered -- cannot link sub-collections into it")
        return

    if start_here_id:
        _link_collection_as_member(
            arango_db,
            parent_collection_id=inbox_seeds_id,
            child_collection_id=start_here_id,
        )
    if platform_artifacts_id:
        _link_collection_as_member(
            arango_db,
            parent_collection_id=inbox_seeds_id,
            child_collection_id=platform_artifacts_id,
        )
