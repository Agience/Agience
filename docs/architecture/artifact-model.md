# Artifact Model and Referencing Architecture

Status: **Reference**
Date: 2026-04-17

---

## Integrated workspace model

Agience workspaces integrate the viewing and editing experience: there is no separate application to launch. Artifacts open as floating windows directly in the workspace.

This makes Agience act as:
- **A browser** — workspaces are tabs, artifacts are documents
- **An IDE** — multiple artifacts open simultaneously
- **A graph viewer** — navigate relationships by opening referenced artifacts

---

## Core concept: artifacts as substrate

Everything in Agience is an artifact — a document with `content` + `context` metadata. Any artifact can be a container (has children via graph edges) or a leaf (no children). Containment is a graph property, not a type property.

### Artifact Display Modes

Each artifact supports multiple presentation modes:

**Small Mode (Grid Tile)**
- Thumbnail view in workspace grid
- Shows: title, type badge, preview snippet
- States: default, hover, selected, dragging
- Primary action: double-click to open

**Large Mode (Floating Window)**
- Full-featured editor in resizable/draggable window
- Shows: complete content with editing tools
- States: view (read-only), edit (active editing), running (for operators)
- Multiple artifacts can be open simultaneously
- Stack order managed by window system (z-index)

### Opening Behavior by Type

**Each artifact type defines its own viewer/editor.**

When you open an artifact (double-click), behavior is determined by `content_type`:

| Type | `content_type` | Visual | Opens As | View Modes |
|------|----------------|--------|----------|------------|
| **Operator** | `application/vnd.agience.transform+json` | ⚡ Purple | Runner window (Palette) | view, edit, running |
| **Text/Markdown** | `text/plain`, `text/markdown` | 📄 Gray | Floating window (markdown editor) | view, edit |
| **Code** | `application/javascript`, etc. | 💻 Green | Floating window (code editor) | view, edit |
| **Image** | `image/*` | 🖼️ Pink | Floating window (image viewer) | view only |
| **Video** | `video/*` | 🎬 Amber | Floating window (video player) | view only |
| **Audio** | `audio/*` | 🎵 Teal | Floating window (audio player) | view only |
| **Workspace** | `application/vnd.agience.workspace+json` | 📁 Purple | Navigate to workspace grid | navigation |
| **Collection** | `application/vnd.agience.collection+json` | 📁 Blue | Navigate to collection view | navigation |
| **MCP Resource** | `application/vnd.mcp.resource+json` | 🔌 Orange | Fetch and show child artifacts | navigation |

**The Type System Architecture:**

1. **Detection**: Examines `content_type` → returns type string
2. **Visual Identity**: Maps type → icon + color
3. **Layout Registry**: Maps MIME → viewer component
4. **Window Behavior**: Routes type → appropriate window/view

This architecture allows:
- **Extensibility**: New types = new viewers without changing core
- **Consistency**: All artifacts of same type behave identically
- **Flexibility**: Same card can render differently in grid vs window

---

## Workspace as context boundary

**Workspace = Browser Tab = Salesforce Tab**

A workspace is your current working context:

**What it tracks**:
- Which artifacts you have open (floating windows)
- Which collection/boundary you're viewing
- What you've pulled in from external sources
- Your navigation history within this context

**Multi-workspace model**:
- Multiple workspaces can be open simultaneously (like browser tabs)
- Each workspace maintains independent state
- Switch between workspaces = switch contexts
- Artifacts can be dragged between workspace tabs

**The Graph Viewer Aspect**:

You're not just editing artifacts - you're navigating a knowledge graph:
```
Open Collection artifact → Shows artifacts inside
├─ Open Operator artifact → Runner window appears
│  ├─ Drop artifacts into Input panel → References added
│  └─ Drop artifacts into Resources panel → Context established
├─ Open referenced artifact → New window opens
└─ Follow links → Navigate graph
```

Each workspace is a "view" into the graph:
- What nodes (artifacts) you're looking at
- What edges (references) you're exploring
- What containers (collections, operators) you've opened

**The Integration Insight**:

Traditional apps:
- File manager shows files
- Text editor edits text
- IDE compiles code
- Database browser queries data
- All separate tools, separate windows, separate mental models

Agience:
- Everything is an artifact
- Open = edit
- No app switching
- Unified interaction model
- "Word integrated into Explorer"

This is why it feels different - you're not "launching apps to work on files". You're "opening artifacts to work on knowledge".

---

## Finding artifacts: three mechanisms

### 1. Browse (Current Context)

**Workspace Grid/List**
- Shows all artifacts in the active workspace
- Ordered by `order_key` (fractional index)
- Filtered by state (`draft`, `committed`, `archived`)
- Source: ArangoDB unified `artifacts` collection (scoped by `collection_id`)

**Collection View**
- Shows committed artifact versions in a collection
- Ordered by relevance or recency
- Source: ArangoDB unified `artifacts` collection (committed state)

**MCP Server Resources**
- Shows resources exposed by connected MCP servers
- Each resource → temporary artifact representation
- Source: MCP server via `/resources/list`

### 2. Search (Global Discovery)

**Workspace-Scoped Search**
- Searches only artifacts in the current workspace
- Implemented via the unified search endpoint (`POST /artifacts/search`) scoped by container
- Frontend also supports lightweight in-memory filtering for currently loaded workspace artifacts

**Global Search**
- Searches across accessible workspaces and collections
- Hybrid: vector similarity (OpenAI embeddings) + BM25 keyword
- Returns artifacts from any source you have access to
- Respects tenant boundaries and access controls

**Search Results**
- Each hit includes `collection_id` (the container — workspaces are also collections)
- Can add search results to current workspace
- Metadata includes `_search.presence` (which containers hold this artifact)

### 3. Reference (Direct Pointers)

**By ID**
- Workspace artifacts: `{artifact.id}` (ArangoDB document key)
- Collection artifacts: `{artifact.root_id}` (stable across versions)
- Reference stored in another artifact's `context` as JSON

**Use Cases**
- Operator panels store arrays of artifact IDs (`input.artifacts`, `resources.artifacts`, etc.)

---

## Artifact lifecycle: workspace vs. collection

### Workspace Artifacts (Ephemeral)

**Where they live**: ArangoDB unified `artifacts` collection (with `state: "draft"`)

**Purpose**: Scratch space for curation before commit

**Key Fields**:
```javascript
{
  id: string,                 // ArangoDB document key
  collection_id: string,      // Container ID (workspace is a collection)
  root_id?: string,           // If pulled from collection
  content: string,            // Text content or binary ref
  context: object,            // JSON metadata (see below)
  state: "draft" | "committed" | "archived",
  content_type?: string,      // MIME type — stored in artifact.context.content_type
  created_time: datetime,
  modified_time: datetime
}
```

**States**:
- `draft` - Created, not yet committed
- `committed` - Committed to a collection
- `archived` - Hidden from main view (soft delete)

**Operations**:
- Create, edit, delete, reorder, archive/restore
- Add to collections (commit)

### Collection Artifacts (Persistent)

**Where they live**: ArangoDB unified `artifacts` collection (with `state: "committed"`)

**Purpose**: Versioned history of committed knowledge

**Key Fields**:
```javascript
{
  _key: string,               // ArangoDB document key
  collection_id: string,      // Target collection
  root_id: string,            // Stable UUID across all versions
  content: string,
  context: object,
  created_by: string,
  created_time: datetime,
  state: "committed"          // or "archived" for soft delete
}
```

**Versioning**:
- Every commit creates a new artifact document with the same `root_id`
- `root_id` stays the same; `_key` is unique per version
- Old versions are never deleted (audit trail)

**Relationships**:
- Artifacts belong to collections via `collection_id`
- Multiple collections can reference the same `root_id`
- When you update a committed artifact, a new version is created

---

## Artifact context: metadata schema

Every artifact has a `context` field (JSON object) with standardized metadata:

```typescript
type ArtifactContext = {
  // Identity & Display
  title?: string;
  filename?: string;
  description?: string;
  tags?: string[];

  // Content Type (drives UI behavior)
  content_type?: string;  // MIME type

  // Source & Access
  content_source?: "agience-content" | "external-url" | "external-api";
  access?: "private" | "public";
  uri?: string;           // For external content
  size?: number;          // File size in bytes

  // Operator-specific (note: field name 'order' is legacy; OrderSpec is the legacy code type name)
  order?: {
    spec: OrderSpec;       // Complete operator configuration
  };

  // MCP-specific (future)
  mcp?: {
    server_id: string;
    resource_uri: string;
  };

  // Upload Progress (transient)
  upload?: {
    status: "in-progress" | "complete" | "error";
    progress: number;     // 0.0 to 1.0
    error?: string;
  };

  // Search Metadata (read-only, added by search)
  _search?: {
    score: number;
    presence: {
      in_current_workspace: boolean;
      workspace_ids: string[];
      collection_ids: string[];
    };
  };
};
```

---

## Referencing model: how artifacts point to artifacts

### Current: Operator Panels (MVP)

**Pattern**: Operator spec stores arrays of artifact IDs (current code field: `artifacts`)

**Example** (Operator artifact context — note: `order` is the legacy code field name for the operator spec):
```json
{
  "type": "transform",
  "title": "Customer Analysis Operator",
  "content_type": "application/vnd.agience.transform+json",
  "order": {
    "spec": {
      "version": 1,
      "panelData": {
        "input": {
          "artifacts": ["artifact-uuid-1", "artifact-uuid-2"],
          "text": "Analyze sentiment"
        },
        "resources": {
          "artifacts": ["artifact-uuid-3"],
          "resources": []
        },
        "prompts": {
          "artifacts": ["artifact-uuid-4"],
          "selectedId": "artifact-uuid-4"
        }
      }
    }
  }
}
```

**Resolution**:
- When loading an operator, look up artifact IDs in the current workspace
- If not found in workspace, optionally fetch from collection (future)
- Display artifact titles in operator panels
- Drag/drop artifacts into panels updates the ID arrays

**Lifecycle:**
- IDs are stable: workspace artifacts keep the same `id`, collection artifacts keep the same `root_id`
- If a referenced artifact is deleted from workspace, the operator panel shows "Artifact not found"
- When you commit an operator artifact, the artifact IDs in the spec are committed as-is

### Container artifacts (workspaces and collections)

**Pattern**: Container artifacts with children linked via `collection_artifacts` edges

A workspace IS a collection IS an artifact. Containers are just artifacts with `content_type` set to `application/vnd.agience.workspace+json` or `application/vnd.agience.collection+json`. Children are linked by graph edges, not by ID lists embedded in context.

**Behavior**:
- Opening a container artifact navigates to its artifact grid
- Dropping a container artifact into an operator means "use all artifacts in this container"
- Any artifact can have children (containment is a graph property)

### MCP resource artifacts

**Pattern**: Server boundary with resource children

**Example** (MCP Server artifact):
```json
{
  "type": "mcp-server",
  "title": "Slack Workspace",
  "content_type": "application/vnd.agience.mcp-server+json",
  "mcp": {
    "server_id": "slack-workspace-abc",
    "resource_count": 15
  }
}
```

**Behavior**:
- Opening an MCP server artifact fetches `/resources/list` and displays as child artifacts
- Each resource becomes a temporary artifact (not saved to workspace until user adds it)
- Dropping an MCP server artifact into an operator means "fetch resources at runtime"

---

## Source artifacts (integration routing)

Some artifact types act as **persistent routing identities** for external integrations — they own their own API key and receive data from outside Agience (live streams, bots, webhooks). The workspace is derived from the artifact, not the other way around.

### Characteristics

- The artifact has its own `agc_*` API key stored as a hash in ArangoDB.
- External services authenticate using artifact ID + API key (`{artifact_id}:{agc_api_key}`) — no workspace ID in the URL.
- The artifact accumulates child artifacts (session artifacts, message artifacts) linked via `context.source_artifact_id`.
- Deleting the artifact revokes the key.

### Current Source Artifact Types

**`application/vnd.agience.stream+json`** (streaming source)
- Represents a streaming media source in the workspace
- Owned by Astra server

### Key Principle

> The workspace is where the artifact lives — not something the external service needs to know.

Source artifacts embody "everything is an artifact": the integration config, auth, and history all live in one artifact that the user can view, duplicate, move, or delete like any other artifact.

**Sharing**: Source artifacts are committed to collections like any other artifact. The viewer renders identically in both contexts — edit actions (key management) gate on access rights, live status gates on `GET /stream/sessions?source_artifact_id=`. Collections are the sharing surface; workspaces are private staging.

---

## Avoiding loops: reference integrity rules

### 1. One-Way Pointers Only

- Artifacts can reference other artifacts via IDs in operator specs (e.g. `input.artifacts`, `resources.artifacts`)
- **No automatic back-references** - if Artifact A references Artifact B, B does not know about A
- To find "who references this artifact?", you must scan all artifacts (expensive, not indexed)

### 2. No Circular References in Containment

- If we implement parent/child hierarchies (e.g., workspace → sub-workspaces):
  - Enforce DAG (directed acyclic graph) at creation time
  - Reject operations that would create cycles (A contains B contains A)
  - Use breadth-first traversal to detect cycles before saving

### 3. Reference Validation (Lazy)

- **At creation time**: No validation - allow dangling references
  - Why: Artifacts may be created before their referenced artifacts exist
  - Why: References may cross workspace/collection boundaries
- **At resolution time**: UI shows "Artifact not found" for missing references
  - User can fix by adding the missing artifact or removing the reference
- **At commit time**: Optionally validate that referenced artifacts exist in target collection

### 4. Snapshot Semantics for Operators

- Operator specs store artifact IDs at the time of operator creation
- If a referenced artifact is updated after the operator is saved, the operator still points to the old content
- To use updated content: re-drag the artifact into the operator panel to update the ID reference

### 5. Deleting Referenced Artifacts

**Workspace Artifacts**:
- Delete is immediate (state → `archived` or hard delete if `draft`)
- Operators/references become dangling pointers (UI shows "Artifact not found")
- No cascade delete - preserve operator spec integrity

**Collection Artifacts**:
- Delete sets `state: "archived"` (soft delete)
- References still resolve to archived artifact (with warning in UI)
- Hard delete is never exposed to users (audit requirement)

---

## Reference patterns: what goes where?

### Workspace Artifacts (Working Set)

**What belongs in workspace**:
- Raw ingested content (files, transcripts, emails)
- Extracted information units (meeting notes, action items)
- Drafts and work-in-progress edits
- Artifacts you're actively curating for this task
- Operator artifacts you're designing/testing

**What does NOT belong**:
- Committed, unchanging knowledge (lives in collections)
- Artifacts you're not actively editing (pull from collection as needed)
- Large archives (commit to collection, remove from workspace)

**Guideline**: Workspace is your "working memory" - keep it focused on the current task

### Operator Panels (Artifact References)

**What goes into operator panels**:
- **Input panel**: The specific artifacts this operator should process
  - Example: "These 5 meeting transcripts"
  - IDs stored in `spec.panelData.input.artifacts[]`
- **Resources panel**: Background knowledge artifacts
  - Example: "Company wiki + product docs"
  - IDs stored in `spec.panelData.resources.artifacts[]`
- **Prompts panel**: Instruction artifacts
  - Example: "Summarization prompt"
  - IDs stored in `spec.panelData.prompts.artifacts[]`

**What does NOT go**:
- Large datasets (use workspace/collection reference instead)
- Frequently changing content (operator won't auto-update)
- Artifacts the operator should discover dynamically (use MCP tools instead)

**Guideline**: Operator panels are "pinned references" - explicit, stable inputs

### Collections (Committed Knowledge)

**What belongs in collections**:
- Curated, reviewed knowledge artifacts
- Source material for retrieval (wiki, docs, FAQs)
- Historical records and audit trails
- Shared knowledge across teams/workspaces

**What does NOT belong**:
- Work-in-progress drafts (keep in workspace)
- Transient runtime data (logs, temp results)
- User-specific scratchpad notes

**Guideline**: Collections are your "long-term memory" - curated, trustworthy, reusable

---

## Examples: real-world use cases

### Use case 1: Meeting analysis operator

**Setup**:
1. Upload 3 meeting transcripts → 3 artifacts in workspace
2. Create an Operator artifact with:
   - Input panel: IDs of the 3 transcript artifacts
  - Prompts panel: ID of "Meeting Summary Prompt" artifact
3. Run the operator → output is new "Meeting Summary" artifact in workspace

**Artifact lifecycle**:
- Transcript artifacts: `state: draft` → commit to "Meetings" collection
- Operator artifact: `state: draft` → commit to "Operators" collection
- Summary artifact: `state: draft` → edit → commit to "Meetings" collection

**References**:
- Operator spec stores: `input.artifacts: ["trans-1", "trans-2", "trans-3"]`

### Use case 2: Customer research workspace

**Setup**:
1. Connect Slack MCP server → add to workspace
2. Open Slack server artifact → browse channels as child artifacts
3. Add specific channels to workspace
4. Tag and organize into "Customer Feedback" collection

**Artifact structure**:
```
Workspace "Q1 Research"
├── MCP Server: Slack (container artifact)
│   ├── #customer-feedback (pulled as child)
│   ├── #support-tickets (pulled as child)
│   └── #feature-requests (pulled as child)
├── Analysis Operator (operator artifact)
│   └── Input: Slack channel artifacts (references)
└── Summary (output artifact)
```

**Commit strategy**:
- Slack message artifacts → commit to "Customer Insights" collection
- Operator artifact → commit to "Operators" collection
- Don't commit MCP server artifact (it's a runtime container)

### Use case 3: Nested workspaces

**Setup**:
1. Main workspace: "2026 Planning"
2. Create child workspace artifacts:
   - "Q1 Goals" workspace
   - "Q2 Goals" workspace
   - "Budget Analysis" workspace
3. Each child workspace contains its own artifacts
4. Create rollup operator that references all child workspaces

**Artifact structure**:
```
Workspace "2026 Planning"
├── Workspace: Q1 Goals (container artifact)
│   ├── Artifact: OKR 1
│   ├── Artifact: OKR 2
│   └── Artifact: Budget
├── Workspace: Q2 Goals (container artifact)
│   └── ...
└── Operator: Quarterly Rollup
    └── Input: Q1 workspace, Q2 workspace (references)
```

**Navigation**:
- Double-click "Q1 Goals" workspace artifact → opens Q1 workspace view
- Breadcrumb: 2026 Planning > Q1 Goals
- Commit: Commit child workspace → commits all artifacts in that workspace

---

## Summary

Artifacts reference other artifacts via IDs. The UI resolves those IDs at open time. This keeps the model simple and flexible: all artifacts use UUID IDs, committed artifacts also have a stable `root_id` across versions, and operator panels store explicit arrays of artifact IDs.
