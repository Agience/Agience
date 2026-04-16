"""
Bootstrap constants — kernel-known-at-init slugs, content types, and list fixtures.

OS analogy: These are the filesystem formats and mount points the kernel must
understand to boot.  The kernel knows these constants at init time so it can
create the initial platform collections and singleton artifacts during startup.

No other Core code should reference content type strings or slug strings directly —
all runtime type resolution flows through the type registry (types_service) and
the ID registry (platform_topology).

Consumed exclusively by seed/bootstrap services:
  - authority_content_service.py
  - host_content_service.py
  - resources_content_service.py
  - inbox_seeds_content_service.py
  - seed_content_service.py
  - llm_connections_content_service.py

Do NOT add new entries here unless the constant is required for platform bootstrap.
Content types that are not created at startup belong to their owning MCP server.
"""

# ---------------------------------------------------------------------------
# Content types — required for bootstrap artifact creation only
# ---------------------------------------------------------------------------

AUTHORITY_CONTENT_TYPE = "application/vnd.agience.authority+json"
HOST_CONTENT_TYPE = "application/vnd.agience.host+json"
AGENCY_CONTENT_TYPE = "application/vnd.agience.agency+json"
AGENT_CONTENT_TYPE = "application/vnd.agience.agent+json"
LLM_CONNECTION_CONTENT_TYPE = "application/vnd.agience.llm-connection+json"
MCP_SERVER_CONTENT_TYPE = "application/vnd.agience.mcp-server+json"
PACKAGE_CONTENT_TYPE = "application/vnd.agience.package+json"

# ---------------------------------------------------------------------------
# Collection slugs — stable human-readable IDs for idempotent bootstrap lookup
# ---------------------------------------------------------------------------

AUTHORITY_COLLECTION_SLUG = "agience-authorities"
HOST_COLLECTION_SLUG = "agience-hosts"
RESOURCES_COLLECTION_SLUG = "agience-resources"
INBOX_SEEDS_COLLECTION_SLUG = "agience-inbox-seeds"

# Seed sub-collections — populated at startup, granted READ to new users on first login
START_HERE_COLLECTION_SLUG = "agience-seeds-start-here"
PLATFORM_ARTIFACTS_COLLECTION_SLUG = "agience-seeds-platform-artifacts"
ALL_SERVERS_COLLECTION_SLUG = "agience-seeds-all-servers"
ALL_TOOLS_COLLECTION_SLUG = "agience-seeds-all-tools"
AGENTS_COLLECTION_SLUG = "agience-seeds-agents"

LLM_CONNECTIONS_COLLECTION_SLUG = "agience-llm-connections"

# Package registry — committed package manifests that have been published
# for discovery. Shown in the marketplace browse UI and queryable via the
# standard artifact search. Empty at first boot; populated as users publish.
PACKAGE_REGISTRY_COLLECTION_SLUG = "agience-package-registry"

# Platform operator — singleton collection whose write grant designates the operator
OPERATOR_COLLECTION_SLUG = "agience-platform-operator"

# ---------------------------------------------------------------------------
# Artifact slugs
# ---------------------------------------------------------------------------

AUTHORITY_ARTIFACT_SLUG = "agience-authority-current-instance"
HOST_ARTIFACT_SLUG = "agience-host-current-instance"
AGENCY_ARTIFACT_SLUG = "agience-agency-platform"
AGENT_ARTIFACT_SLUG_PREFIX = "agience-agent-"
LLM_CONNECTION_SLUG_PREFIX = "agience-llm-"
# Phase 7 — Server Artifact Proxy. First-party MCP servers are seeded as
# vnd.agience.mcp-server+json artifacts at bootstrap. Slug format: agience-server-{name}
# (matching the client_id used by kernel server credentials).
SERVER_ARTIFACT_SLUG_PREFIX = "agience-server-"

# ---------------------------------------------------------------------------
# Agent persona slugs (used to derive artifact slugs)
# ---------------------------------------------------------------------------

PLATFORM_AGENT_SLUGS = [
    "aria", "astra", "atlas", "sage",
    "nexus", "ophan", "seraph", "verso",
]

PLATFORM_LLM_CONNECTION_SLUGS = [
    "anthropic-sonnet",
    "anthropic-opus",
    "openai-gpt4o",
    "openai-gpt5-nano",
]

# ---------------------------------------------------------------------------
# Seed collection fixture lists
# ---------------------------------------------------------------------------

# Collections granted READ to every new user on first login
USER_READABLE_SEED_SLUGS = [
    INBOX_SEEDS_COLLECTION_SLUG,
    START_HERE_COLLECTION_SLUG,
    PLATFORM_ARTIFACTS_COLLECTION_SLUG,
    ALL_SERVERS_COLLECTION_SLUG,
    ALL_TOOLS_COLLECTION_SLUG,
    AGENTS_COLLECTION_SLUG,
    LLM_CONNECTIONS_COLLECTION_SLUG,
    PACKAGE_REGISTRY_COLLECTION_SLUG,
]

# Collections whose committed artifacts are materialized into the user's inbox workspace.
# Inbox Seeds currently contains curated collection artifacts (for example Start Here and
# Platform Artifacts). Their member artifacts remain in their own collections and are not
# flattened into every inbox workspace.
INBOX_MATERIALIZATION_SLUGS = [INBOX_SEEDS_COLLECTION_SLUG]

# All platform-owned collections (used for admin grants and ID pre-resolution)
ALL_PLATFORM_COLLECTION_SLUGS = [
    AUTHORITY_COLLECTION_SLUG,
    HOST_COLLECTION_SLUG,
    RESOURCES_COLLECTION_SLUG,
    INBOX_SEEDS_COLLECTION_SLUG,
    START_HERE_COLLECTION_SLUG,
    PLATFORM_ARTIFACTS_COLLECTION_SLUG,
    ALL_SERVERS_COLLECTION_SLUG,
    ALL_TOOLS_COLLECTION_SLUG,
    AGENTS_COLLECTION_SLUG,
    LLM_CONNECTIONS_COLLECTION_SLUG,
    PACKAGE_REGISTRY_COLLECTION_SLUG,
    OPERATOR_COLLECTION_SLUG,
]
