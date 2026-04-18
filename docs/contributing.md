# Contributing to Agience

Status: **Reference**
Date: 2026-04-01

Thank you for your interest in contributing to Agience. This document covers everything you need to know before opening a pull request: the CLA requirement, development setup, contribution workflow, code standards, and issue conventions.

---

## Before You Contribute

### Contributor License Agreement

All contributors must sign the Agience Contributor License Agreement (CLA) before any code can be merged. This is a hard requirement — no exceptions.

The CLA is enforced automatically via the CLA bot on GitHub. When you open your first pull request, a bot comment will prompt you to sign. The process takes under a minute.

**What the CLA does:**

- You retain copyright over your contribution. Your code is still yours.
- You grant Agience a broad, irrevocable license to use, reproduce, modify, distribute, sublicense, and commercialize your contribution as part of the project.
- You can use your own code in any other project, fork, or commercial product — the grant is to Agience, not an assignment of ownership.

**Why it is required:**

Agience is dual-licensed (open-source AGPL-3.0 plus a commercial tier). Without a CLA, contributions cannot legally be included under the commercial license. The CLA is what makes it possible to sustain both.

Full CLA text is in `CLA.md` at the repo root. Read it before signing.

**CLA scope distinction:**

- **Core contributions** — backend, frontend, and platform infrastructure. The signed CLA grants Agience broad rights, as described above.
- **MCP server contributions** — code submitted to `mcp-servers/` in this repo. The CLA still applies to code submitted here, but you retain full rights and can publish your server independently under any license you choose. Each MCP server ships its own `LICENSE` file.

---

## Development Setup

For local environment setup including Docker prerequisites, env configuration, and how to run the full stack or dev-mode (infra only), see the [Local Development Guide](getting-started/local-development.md).

---

## Contribution Workflow

### For code contributions

1. **Fork the repository** (external contributors) or create a branch from `main` (maintainers).
2. **Sign the CLA** — the bot will prompt you on your first PR if you have not already signed.
3. **Read the architecture instructions** in `CLAUDE.md` and `backend/CLAUDE.md` (or `frontend/CLAUDE.md`) before making structural changes. The layered architecture and decision tests are non-negotiable.
4. **Make your changes** — follow the conventions in [Best Practices](guides/best-practices.md).
5. **Write tests** — see the testing expectations below. New endpoints and non-trivial logic require tests.
6. **Run checks locally** before pushing:
   ```bash
   # Backend
   cd backend
   ruff check .
   pytest tests/

   # Frontend
   cd frontend
   npm run lint
   npm run test
   ```
7. **Open a pull request** — fill in the PR template checklist.
8. **Address review feedback** — maintainers review for correctness, style, and architectural fit.

### For MCP server contributions

Agience is designed to be extended via external MCP servers. If you are building a server that integrates with Agience:

1. Create your server in `mcp-servers/<your-server>/` following the structure in `servers/CLAUDE.md`.
2. Include a `LICENSE` file specific to your server.
3. Follow MCP protocol conventions — your server should work with any MCP client, not just Agience.
4. Register your server in a workspace as a `vnd.agience.mcp-server+json` artifact for testing.
5. You retain full rights to publish and license the server independently.

### For documentation contributions

- Follow the header convention: `# Title` then `Status: **Draft|Reference|Canonical**` and `Date: YYYY-MM-DD`.
- Public-facing documentation goes in `docs/`.
- Do not document every fix. Only update docs for new features, changed contracts, or behavior that users or contributors need to understand.
- If you change a backend API shape or a content type definition, update the relevant public doc in tandem.

---

## Pull Request Guidelines

### Title format

Use concise imperative titles that describe what the PR does:

```
Add stream key rotation to Astra MCP server
Fix fractional index ordering on workspace reorder
Update MCP server registration to use well-known endpoint
```

Avoid: `Fixed the thing`, `Various improvements`, `WIP`.

### Description template

Every PR should include:

- **What changed** — a brief description of what this PR does
- **Why** — the motivation or issue it addresses
- **Test plan** — what you tested and how; include specific test commands or test file names
- **Checklist**:
  - [ ] CLA signed
  - [ ] Tests pass (`ruff check . && pytest tests/` in backend; `npm run lint && npm run test` in frontend)
  - [ ] No secrets or credentials in diff
  - [ ] Documentation updated (if the change affects user-facing behavior or public contracts)
  - [ ] Architectural decision tests (D1–D8) checked if the change touches layer boundaries

### What reviewers check

- Correctness and test coverage
- Adherence to the layered architecture (Core / Handlers / Presentation — see `CLAUDE.md`)
- Vocabulary consistency (Artifact not Card in data models; see the vocabulary table in `CLAUDE.md`)
- No type-specific logic in Core or Presentation layers
- No DB calls bypassing the service layer
- No new `vnd.*` entries in `frontend/src/content-types/` — new viewers go on MCP servers

---

## Code Standards

Full conventions are in [Best Practices](guides/best-practices.md). Key rules:

**Backend (Python)**

- Python 3.11+. Follow existing patterns in `backend/`.
- Run `ruff check .` from `backend/` and fix all issues before pushing.
- Never call DB adapters directly from routers — all DB access goes through services.
- Use `to_dict()` on all entities. `Collection` is an alias for `Artifact` — there are no separate entity classes or serialization methods.
- Use `agent_service.invoke()` for all agent calls. Never call agents directly.

**Frontend (TypeScript / React)**

- Run `npm run lint` from `frontend/` and fix all issues.
- Always use typed API functions from `frontend/src/api/` — never `fetch()` directly.
- Presentation components must not import from `content-types/` packages or check `contentType.id`.
- All type-specific wiring flows through `frontend/src/registry/`.

**Tests**

- Backend: router tests required for every new or changed endpoint; unit tests for non-trivial service/agent/search logic.
- Frontend: Vitest + RTL tests required for UI/UX behavior changes.
- Never hit real ArangoDB, OpenSearch, or S3 in tests. Mock at the service layer.
- Mock all external HTTP calls.

---

## Issue Conventions

### Bug reports

Include:
- Steps to reproduce (minimal and specific)
- Expected behavior
- Actual behavior
- Environment (local dev / Docker / hosted, browser if frontend)
- Relevant log output

Use the **bug report** issue template.

### Feature requests

Include:
- The use case or problem you are solving
- Your proposed approach (if you have one)
- Which layer of the system this affects (Core / Handler / Presentation)

Use the **feature request** issue template.

### MCP server proposals

If you want to propose or register a new first-party or community MCP server, use the **MCP server proposal** template. Include: what the server does, the intended tool surface, and your licensing intent.

### Good first issues

Issues tagged `good-first-issue` are starter tasks suitable for new contributors. They typically cover documentation improvements, test coverage gaps, small bug fixes, and UI polish items.

---

## Commit Message Format

Agience uses [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types**: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `style`

**Scope** (optional): the area of the codebase — e.g., `backend`, `frontend`, `aria`, `astra`, `atlas`, `nexus`, `search`, `auth`

**Examples:**

```
feat(astra): add document_text_extract tool to Astra MCP server
fix(search): correct RRF fusion weight when aperture filter is active
docs(atlas): add governance content types to Atlas solution page
test(backend): add router tests for inbound webhook endpoint
chore(deps): update FastMCP to 1.4.0
```

Breaking changes should include `BREAKING CHANGE:` in the footer, or append `!` after the type: `feat!: rename workspace artifact state field`.

---

## Documentation

### When to update docs

Update public docs (`docs/`) when:
- A user-facing feature is added or changed
- An API contract, content type, or configuration variable changes
- A getting-started or setup flow changes

Do not document every bug fix or internal refactor.

### Which docs to update

| Changed area | Doc to update |
|---|---|
| New MCP server or tool | `docs/overview/solution-taxonomy.md` |
| API endpoint added or changed | `docs/api/README.md` (if the endpoint group is new) |
| Content type added | `docs/overview/platform-overview.md` |
| Auth or security change | `docs/getting-started/self-hosting.md` if deployment-relevant |
| New getting-started flow | `docs/getting-started/` |
| New feature shipped | `docs/changelog.md` |
