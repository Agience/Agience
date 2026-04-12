# Agience

**The operating system that AI workflows trust.**

Agience turns AI-generated output into durable, governed knowledge artifacts with identity, provenance, and version history so humans and agents can safely collaborate on the same information substrate.

---

## Why Agience

AI generates output. It doesn't create trust. Any LLM can produce summaries, drafts, and decisions at scale — but without identity and provenance, those outputs have no chain of custody and no reason for anyone downstream to build on them.

Agience is what turns AI output into something an organization can actually rely on. When an agent ingests a transcript, the result is a set of typed artifact objects — decisions, actions, constraints — committed into versioned collections under human review. The same object a human edits in the UI is the object an agent reads over MCP. Accountability follows from the architecture, not from policies or prompts.

---

## Core Properties

**Artifacts, not files.**
Every object — document, transcript, agent config, MCP server registration, collection entry — is an artifact. Typed content, structured context, stable ID, full version history, and a record of what produced it.

**Human-in-the-loop is structural.**
Approval gates are first-class operators in any workflow, not a policy layer you bolt on later. The architecture enforces the boundary; it does not rely on prompts.

**MCP-native throughout.**
Agience is both an MCP server (exposing tools to VS Code, Claude Desktop, Cursor, or any compatible client) and an MCP client (consuming GitHub, Slack, filesystem, and other vendor MCP servers). 

**Trust is declared, not assumed.**
Scoped API keys define exactly what each agent or server can read, write, or invoke. Identity comes from the auth token, never the request body. Delegated operations carry a record of who authorized them.

**Provenance is infrastructure.**
Like a filesystem journal, provenance in Agience is structural. Committed artifacts carry records of what produced them, from what inputs, under whose authority. Not a premium feature — a consequence of how the system is built.

**Composable agent servers.**
The platform ships with eight purpose-built MCP servers covering ingestion, retrieval, reasoning, output, networking, security, governance, and finance. Each is a standalone FastMCP service. Deploy the ones you need, replace the ones you don't.

---

## The OS Analogy

| OS Concept | Agience |
|---|---|
| File records / inodes | Artifacts |
| Windows / explorer views | Cards (UI layer) |
| File extensions | MIME content types |
| Working directory | Workspace |
| Published filesystem | Collection |
| Save / publish | Commit |
| Kernel services | Core platform |
| Peripheral drivers | Agent persona servers |
| Third-party applications | External MCP servers |
| Processes / jobs | Agents / transforms |
| System calls | MCP tool calls |
| Filesystem indexer | OpenSearch |
| Capability-based access | Scoped API keys |
| Change journal | Provenance chain |

---

## What Ships Today

- **Artifact model** — typed, versioned objects with stable IDs, full history, and graph relationships
- **ArangoDB architecture** — all artifact storage in ArangoDB; workspaces for ephemeral drafts, collections for committed versions
- **Commit flow** — explicit workspace → collection promotion; nothing published silently
- **Hybrid search** — BM25 + kNN vector search with RRF fusion, aperture filtering, and per-token semantic modifiers
- **Multi-provider OAuth2** — Google, Microsoft Entra, Auth0, custom OIDC, username/password; RS256 JWT + scoped API keys
- **MCP server** — 11 tools at `/mcp`; advertised via `/.well-known/mcp.json`; works in VS Code, Claude Desktop, Cursor
- **Agentic chat loop** — chat artifacts with an 8-tool surface and multi-turn LLM loop
- **Live stream ingestion** — OBS → SRS → real-time AWS Transcribe; transcript artifacts committed on stream end
- **S3/CloudFront media handling** — direct browser-to-S3 presigned upload, signed CDN delivery, orphan cleanup
- **Eight agent persona servers** — Astra (ingestion), Sage (retrieval), Verso (reasoning), Aria (output), Nexus (comms), Atlas (governance), Seraph (security), Ophan (finance)

See [ROADMAP.md](ROADMAP.md) for the full capability inventory and what's coming next.

---

## Getting Started

### Run Agience (canary build — no build tools required)

Pull and run the latest pre-built images directly from this repo. No Node, Python, or build tools needed — just Docker.

```
git clone https://github.com/Agience/agience-core.git
cd agience-core
agience canary
```

**Linux / macOS:**
```bash
./agience canary
```

Images are tagged `:canary` and published automatically on every merge to `main`. The setup wizard runs on first boot.

To force-pull the latest images after an update:
```
agience canary -f
```

### Run Agience at Home (stable build)

For a stable, pre-configured home deployment using pinned release images, clone the [agience-home](https://github.com/Agience/agience-home) repo and follow the setup instructions there.

The one-command hosted installer (`get.agience.ai`) is coming soon.

### Developer Setup (build from source)

For contributors and developers who want to modify the platform:

```bash
git clone https://github.com/Agience/agience-core.git
cd agience-core
```

**Windows:**
```
agience dev -f --build
```

**Linux / macOS:**
```bash
./agience dev -f --build
```

This starts infrastructure in Docker, installs dependencies into a `.venv`, and launches the backend + frontend locally. The setup wizard handles OAuth and LLM configuration on first boot — no manual `.env` file required.

Full developer guide: [`docs/getting-started/local-development.md`](docs/getting-started/local-development.md)

### Hosted

Sign up at [agience.ai](https://agience.ai) — no setup required.

---

## Architecture

```
  ┌─────────────────────────────────────┐
  │  Presentation (React / Vite)        │  Cards, grid, windows, navigation
  └─────────────────────────────────────┘
                    │ registry
  ┌─────────────────────────────────────┐
  │  Handlers (MCP Servers)             │  Type-specific viewers, tools, prompts
  │  Aria · Astra · Atlas · Sage        │  Served as ui:// MCP resources
  │  Nexus · Ophan · Seraph · Verso     │  Each a standalone FastMCP process
  └─────────────────────────────────────┘
                    │ MCP
  ┌─────────────────────────────────────┐
  │  Core (FastAPI)                     │  Type-agnostic platform services
  │  Auth · Artifacts · Workspaces      │  ArangoDB · OpenSearch
  │  Collections · Search · MCP infra  │  S3 · JWT · Scoped API keys
  └─────────────────────────────────────┘
```

Architecture spec: [`.dev/features/layered-architecture.md`](.dev/features/layered-architecture.md)

### Repo layout

```
backend/        FastAPI Core — type-agnostic platform services
frontend/       React + Vite + Tailwind UI
servers/        First-party MCP persona servers
servers/_host/  Unified Docker mount for all personas
types/          Builtin MIME type definitions
hosts/          Desktop companion relay runtime
docker/         Compose files and Caddy config
docs/           Public-facing documentation
```

---

## Documentation

| | |
|---|---|
| [Platform Overview](docs/overview/platform-overview.md) | What ships and how it fits together |
| [Self-Hosting Guide](docs/getting-started/self-hosting.md) | Deploy on your own infrastructure |
| [MCP Setup](docs/mcp/client-setup.md) | Connect VS Code, Claude Desktop, Cursor |
| [Search Query Language](docs/reference/search-query-language.md) | `+required`, `~semantic`, `type:`, `tag:` operators |
| [MCP Overview](docs/mcp/overview.md) | Full MCP server and client architecture |
| [ROADMAP.md](ROADMAP.md) | Shipped and in-progress capabilities |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guidelines and CLA |

---

## Contributing

Bug reports, documentation improvements, new agent tools, and thoughtful feature proposals are welcome.

1. Read [CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md)
2. Open an issue before writing code for new features
3. Security issues → **security@agience.ai** (do not open a public issue)

---

## License

Agience Core is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0-only).

- **Free use**: open-source and AGPL-compliant deployments, including network-accessible services that share source
- **Commercial license required**: proprietary/closed-source use, managed services without source disclosure, OEM/embedded distribution, white-label use

See [LICENSE.md](LICENSE.md).
