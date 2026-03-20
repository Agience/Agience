# Agience

> **This repository is being prepared for public release. Full documentation, setup guides, and contribution workflows are coming soon.**

**Agience** is an open, extensible runtime where AI agents and humans read and write shared knowledge with validation, authority, and accountability built into the architecture.

Think of it as Git + a graph database + an agent runtime, but for decisions and knowledge instead of code.

---

## What This Is

Most AI tooling gives you a chat interface on top of documents. Agience gives you a **shared state layer**: typed, versioned, auditable information that both humans and agents can read, write, and reason about within the same system.

The core unit is the **artifact**: not a file, not a prompt, not a chat message. An artifact has typed content, structured metadata, a stable ID, a commit history, and a reference to the transform that produced it. Agents and humans operate on the same artifacts. The same object a human curates in the UI is the object an agent reads via MCP.

When state is shared and structured, accountability follows from the architecture, not from prompts or policies. The system tracks what was done, by whom, and under what authority.

---

## Core Properties

**Shared state, not shared documents.**
Artifacts are the primary interface between humans and agents. Not files. Not conversation history. Typed objects with stable identities, versioned content, and explicit relationships.

**Human-in-the-loop is structural, not bolted on.**
The commit is a gated, explicit act. Human approval steps are first-class operators in any workflow, not a policy layer you add later.

**MCP-native.**
Agience is both an MCP server (exposing tools to VS Code, Claude Desktop, or any compatible client) and an MCP client (consuming vendor MCP servers). It does not re-implement what official vendor servers provide.

**Trust is declared, not assumed.**
Scoped API keys define exactly what each agent or server can read, write, or invoke. Identity comes from the auth token, never the request body. Delegated operations carry a record of who authorized them.

**Provenance is infrastructure.**
Like a filesystem journal or database transaction log, provenance in Agience is structural. Committed artifacts carry records of what produced them, from what inputs, under whose authority. It is not a premium feature, it is a consequence of how the system is built.

**Composable agent servers.**
The platform ships with purpose-built MCP servers covering ingestion, retrieval, reasoning, output, networking, security, governance, and finance. Each is a standalone FastMCP service. Deploy the ones you need.

---

## The OS Analogy

Agience works like an operating system for knowledge:

| OS Concept | Agience Equivalent |
|---|---|
| Inodes / file records | Artifacts |
| Windows / explorer views | Cards (UI) |
| File extensions | Content types (MIME) |
| Working directory | Workspace |
| Published filesystem | Collection |
| Save / publish | Commit |
| Kernel services | Agent servers |
| Peripheral drivers | Third-party MCP servers |
| Processes / jobs | Agents / transforms |
| System calls | MCP tool calls |
| File creator / program ID | Transform identifier
| Filesystem indexer | OpenSearch |
| Capability-based access | Scoped API keys |
| Change journal | Provenance chain |

Everything runs on shared, structured knowledge instead of files or prompts.

---

## Licensing

Agience Core is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0-only).

- **Free use**: compliant with AGPL, including sharing source for modifications and network-accessible deployments
- **Commercial license required**: proprietary/closed-source use, managed services without source disclosure, OEM/embedded distribution, or white-label use

See [LICENSE.md](LICENSE.md) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md).

---