# Agience Platform Overview

Status: **Reference**
Date: 2026-04-01

Agience is an open-source, MCP-native knowledge platform designed to turn live work into durable, attributable truth.

At the practical level, Agience helps teams capture content from meetings, documents, media, chats, and external systems; run AI-assisted workflows and automation over that material; curate the results with humans in the loop; and commit reviewed knowledge into a durable system of record.

At the architectural level, Agience is a **runtime for organizational knowledge systems**. It captures operational signals from real work, extracts candidate reasoning artifacts, validates them through human interaction, and commits them into a structured, versioned collection representing evidence, decisions, and outcomes.

The core product decision: AI can help produce structure, but reviewed knowledge should not silently emerge from raw output. That separation between draft and trusted knowledge is the foundation of everything else.

---

## The problem

Most enterprise systems capture outputs — documents, tickets, messages, reports. Most teams already know how to generate more output. The harder problem is preserving the reasoning, evidence, and accountability behind it.

Important context is often created in real time and then flattened:

- decisions are made in meetings and reduced to partial notes
- follow-up documents keep conclusions but lose tradeoffs
- AI summaries accelerate output while weakening traceability
- teams end up operating on degraded copies of their own reasoning

Agience captures the reasoning chain that produced outputs, not just the outputs themselves. The canonical reasoning structure:

```
evidence → claim → constraint → decision → action → receipt
```

These units become artifacts in versioned collections. Documents and reports become derived projections of the committed artifact collections.

Agience is built to preserve the path from raw input to reviewed knowledge.

---

## What teams actually do in Agience

Agience is not just a place to store knowledge. It is a working surface for capture, analysis, coordination, and review.

Teams can use it to:

- ingest files, transcripts, recordings, messages, and external events into one workspace
- search across draft and committed knowledge with hybrid semantic and keyword retrieval
- run AI-assisted extraction, synthesis, chat, and research tasks over artifacts
- define operators and automation that turn incoming events into structured artifacts
- connect external MCP servers and vendor tools without rebuilding those integrations inside Agience
- review, refine, and approve outputs before they become durable knowledge

The result is a platform that supports both day-to-day operational work and longer-lived knowledge curation.

---

## Core model

### Workspaces

Work happens in **Workspaces**.

Workspaces are fast, editable, high-churn environments where teams can:

- ingest files, transcripts, and media
- receive inbound events and operator artifacts
- run AI extraction and synthesis
- run chats, operators, and automation against live context
- reorganize and refine artifacts
- collaborate before anything becomes canonical

Workspaces serve a dual surface: humans see **cards** (visual presentation), agents see **artifacts** (addressable data).

### Collections

Truth lives in **Collections**.

Collections are durable, versioned, and auditable. They are the long-term memory layer for approved knowledge.

### Commit

Moving content from a workspace into a collection requires an explicit **commit**.

That friction is intentional. It is the product mechanism that keeps draft output from silently becoming doctrine.

The commit lifecycle:

```
observation
→ candidate artifact (workspace draft)
→ validation interaction (human curation in workspace)
→ commit (explicit act)
→ collection (canonical versioned truth)
```

AI proposes artifacts. Humans confirm them through workspace curation — reviewing insights rather than authoring from scratch.

### Artifacts

Everything in Agience is an **Artifact**.

An artifact can represent a transcript, decision, action item, document, synthesis, operator definition, or tool surface. Each artifact carries content, context, and transform information, with provenance attached so the artifact remains inspectable over time.

Artifacts are described by the **Information Triangle**: Content (what it is), Context (where it fits), and Operator (how it is produced or transformed).

In the UI, artifacts are presented as **cards** — visual components that humans interact with.

> **Design principle**: Humans look at Cards. Agents look at Artifacts.

Artifacts are immutable once committed to collections. Workspace artifacts can be freely edited.

---

## System architecture

Agience is composed of four layers:

```
Applications
(meeting decisions, incident investigation, etc.)

Platform Runtime
(artifact graph, validation system, reasoning queries)

Server Runtime
(domain-specific reasoning modules)

Kernel
(storage, extraction pipeline)
```

### Storage

```
ArangoDB          → Workspaces (ephemeral drafts, high-churn editing)
                  → Collections (committed, versioned, immutable history)
                  → Grants, API keys, commits, commit items
S3-compatible     → Media/document content storage
OpenSearch        → Hybrid BM25 + kNN search (OpenAI embeddings)
```

ArangoDB is the sole database. Workspaces hold ephemeral draft state; collections hold durable committed truth. Both share the same ArangoDB instance with separate document collections.

---

## Artifact model

Artifacts are the core unit of knowledge. Every meaningful piece of reasoning or operational activity is stored as an artifact.

### Schema

```json
{
  "id": "uuid",
  "title": "string",
  "content_type": "MIME type",
  "content": "payload (text or structured)",
  "context": {
    "semantic": {
      "kind": "decision | constraint | action | claim",
      "sources": ["artifact_id references"],
      "evidence": ["extracted quotes"]
    },
    "transform": "transform_artifact_uuid (reference, not embedded)"
  },
  "state": "draft | committed | archived",
  "created_at": "timestamp",
  "updated_at": "timestamp"
}
```

Semantic metadata lives in `context.semantic`. Provenance is captured through commit records and source attribution links.

### Artifact states

Artifact states:

| State        | Meaning                                    |
| ------------ | ------------------------------------------ |
| draft        | Created, not yet committed                 |
| committed    | Committed to a collection                  |
| archived     | Marked for removal                         |

### Artifact types

The platform defines a minimal reasoning ontology via content types.

**Reasoning primitives** (expressed via `semantic.kind`):
`evidence`, `claim`, `constraint`, `decision`

**Operational artifacts**:

```
transform      application/vnd.agience.transform+json
agent          application/vnd.agience.agent+json
chat           application/vnd.agience.chat+json
stream         application/vnd.agience.stream+json
mcp-server     application/vnd.agience.mcp-server+json
```

Servers can define additional types via the `inherits` mechanism in the content type system, extending platform primitives with domain-specific metadata.

---

## Capability areas

### Capture and ingest

Agience can bring in information from multiple entry points:

- browser uploads for documents and media
- MCP-connected tools and external systems
- workspace automation and event-driven processing

This makes the workspace a live intake layer, not just a static repository.

Extraction pipeline:

```
signal
→ ingestion (workspace)
→ LLM extraction (extract_units)
→ candidate artifacts
→ validation (workspace curation)
→ commit
```

### Search and research

Agience combines keyword and semantic retrieval so teams can find relevant material across both active workspaces and committed collections.

That search layer supports:

- direct retrieval of artifacts and source material
- evidence-backed research and synthesis
- chat and question-answering grounded in available context

Search is hybrid BM25 (lexical) + kNN (semantic) with RRF fusion via OpenSearch and OpenAI embeddings.

### Operators and automation

Agience can represent operator definitions directly as artifacts (Operator artifacts) and use MCP-connected tools to drive multi-step work.

That includes:

- AI-assisted extraction and synthesis passes
- event-driven processing from inbound systems
- reusable tool-connected operator steps
- human review before durable commit

The practical point is not "automation for its own sake." It is to keep operational work, AI output, and reviewed knowledge in one inspectable system.

### Governance and trust

Agience keeps provenance, actor history, and version lineage attached to the artifacts that matter.

That means teams can review not just what was produced, but how it was produced and what evidence supports it.

---

## Provenance and accountability

Agience is designed as an accountability layer for AI-assisted work.

If a claim exists in the system, the goal is to preserve enough structure to answer:

- where it came from
- what source material supports it
- what transformation produced it
- which actor or agent touched it
- what changed over time

In Agience, provenance is not an afterthought. It is structural metadata carried with durable artifacts. Commits and commit items provide an audit trail of what was committed, when, and by whom.

---

## MCP-native by design

Agience uses the Model Context Protocol so knowledge and operations are not trapped inside one UI or one vendor stack.

The platform acts as both:

- an **MCP server**, exposing Agience tools and resources
- an **MCP client**, connecting to external MCP servers and vendor ecosystems

The product rule is official-first: if a vendor already publishes an MCP server, Agience integrates with it rather than re-implementing the vendor API.

---

## Server runtime

Servers extend the platform via MCP. Servers define artifact types (via the content type system), tools (MCP tool surface), and operators (Operator artifacts).

Eight purpose-built MCP servers ship with the platform: **Astra** (ingestion), **Sage** (research), **Atlas** (governance), **Verso** (reasoning), **Aria** (output), **Nexus** (networking), **Seraph** (security), **Ophan** (finance).

Artifact types can be derived from platform primitives using the `inherits` field in `type.json`. This preserves core semantics while allowing domain-specific extensions.

---

## Security model

Permissions implemented via:

- Collection grants (read/write/admin access)
- Scoped API keys (`resource|tool|prompt : mime : action` format)
- JWT-based auth (RS256, multi-provider OAuth2)
- Workspace isolation (per-user workspaces)

---

## What Agience includes

- Artifact-based knowledge model with durable IDs, metadata, and provenance
- Workspaces and collections with an explicit commit boundary
- Hybrid semantic and keyword search
- AI-assisted extraction and synthesis flows
- Operator-based automation and event-driven processing
- MCP server and client integration
- Multi-provider authentication
- Chat-based interaction with the knowledge base
- Direct browser-to-cloud file handling for larger content

---

## Deployment choices

Agience is built to support both hosted and self-hosted use.

- **Hosted preview** gives new users a low-friction evaluation path
- **Self-hosting** gives organizations direct control over their environment and data
- **Local MCP setup** allows Agience to connect to development tools such as VS Code and Claude Desktop

Hosted and self-hosted paths are both part of the product story. The platform is intentionally not designed around permanent dependency on a single hosted vendor.

---

---

## Recommended next reads

- [Agience Manifesto](manifesto.md)
- [Information OS Analogy](information-os-analogy.md)