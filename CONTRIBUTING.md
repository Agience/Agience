# Contributing to Agience

Thank you for considering a contribution to Agience. We welcome bug reports, documentation improvements, new agent tools, and thoughtful feature proposals.

---

## Before You Start

**Read the [Contributor License Agreement (CLA)](CLA.md).** All contributions require a signed CLA. Signing is one-time in your pull request through the CLA Assistant bot.

**Read the [developer guide](.dev/development/developer-guide.md)** and **[architecture instructions](.github/copilot-instructions.md)** before writing code. 

---

## How to Contribute

### Reporting Bugs

Open an issue with:
- A clear title and description of the unexpected behavior
- Steps to reproduce
- Expected vs. actual behavior
- Environment (OS, Python version, Docker version, browser if frontend)

### Security Issues

**Do not open a public issue for security vulnerabilities.** Email **security@agience.ai** with a description and reproduction steps. We will respond within 5 business days.

### Suggesting Features

Open an issue first. Features without an issue are unlikely to be accepted as pull requests. Describe:
- The problem you are solving (not just the feature you want)
- How it fits the platform model (artifacts, agents, workspaces, collections)
- Whether you intend to implement it

### Submitting Code

1. External contributors should fork the repository and create a branch from `main`
2. Maintainers and solo founders can usually work directly on `main` and use branches only for risky experiments, long-running work, or release stabilization
3. Make your changes (see Code Conventions below)
4. Write or update tests when the change warrants them (see Testing below)
5. Sign off every commit (`git commit -s`) when contributing through the public workflow
6. Open a pull request against `main` when review, collaboration, or release staging adds value

### Solo Founder Workflow

For pre-MVP founder-led development, optimize for coherence and speed rather than ceremony.

- Default path: work on a long-lived personal lane such as `dev/john`, `rc/work`, or another branch that reflects your current primary focus.
- Promote into `main` when the branch has reached a coherent checkpoint worth integrating.
- Use additional temporary branches only when work is unusually risky, highly exploratory, or likely to diverge from your current lane.
- Cut `release/*` branches only when you want to stabilize a candidate release.
- Do not force yourself to open PRs for every change if you are the only decision-maker.

Use this simple rule:

- If the work fits your current stream of attention: commit to your personal lane.
- If the work is coherent enough to become the new shared tip: merge into `main`.
- If the work is messy, reversible, or likely to branch in multiple directions beyond your current lane: use a temporary side branch.
- If a build is worth hardening for external users or demos: cut `release/*`.

### Commit Standard

Use a lightweight conventional format so history stays searchable without adding much overhead:

- `fix: remove hardcoded AWS region from stream gateway image`
- `feat(search): add hybrid weighting preset selection`
- `docs: clarify release image tagging model`
- `refactor(workspace): simplify commit orchestration path`

Recommended types:

- `feat`: user-visible capability or new platform behavior
- `fix`: bug fix or constraint correction
- `refactor`: structural change without intended behavior change
- `docs`: documentation-only update
- `test`: tests added or adjusted
- `chore`: operational or maintenance work

When useful, add a short body with these headings:

- `Why:` what changed in understanding or what problem was addressed
- `What:` the concrete implementation change
- `Follow-up:` anything intentionally deferred

Example:

```text
fix(stream): stop baking AWS region into the gateway image

Why:
The image should not assume a deployment region.

What:
Removed the hardcoded Dockerfile environment variable and rely on runtime injection.

Follow-up:
Ensure deploy repos pass AWS_REGION explicitly.
```

The goal is not rigid compliance. The goal is that future-you can recover intent quickly through search.

### PR Standard

For solo work, PRs are optional. When you do use them, keep them lightweight.

- Your normal path can be: commit to `dev/john` or `rc/work`, then merge into `main` without a PR when you have enough coherence.
- Use a PR for release branches, larger refactors, external contributions, or whenever you want a durable review artifact.
- Use a draft PR as a searchable checkpoint if AI or future-you will need to reconstruct the thread later.
- If you skip the PR, put the same information into the commit body when it matters.

The repository includes a PR template and a commit message template to keep this low effort.

### Promotion To Main

Treat promotion into `main` as the canonical narrative checkpoint.

Before merging your personal lane into `main`, capture a short summary that answers:

- What changed
- Why it matters now
- Which domains or constraints were affected
- What follow-up remains intentionally deferred

Use this checklist:

- [ ] The branch is coherent enough to become the current shared tip
- [ ] CI passed or the risk is understood and accepted
- [ ] The merge summary explains `Why`, `What`, `Affects`, and `Follow-up`
- [ ] Any important Agience artifacts, reports, or issues are linkable from the summary

The repository includes a promotion helper script and a dedicated promotion message template:

- `.scripts/prepare_main_promotion.py`
- `.gitmessage-main.txt`

The same helper also supports external contributors and PR-based workflows:

- `python .scripts/prepare_main_promotion.py --mode pr`
- writes `.git/PULL_REQUEST_BODY.md` with a prefilled PR summary

Suggested one-time Git setup:

```bash
git config commit.template .gitmessage.txt
git config alias.promote-note "!python .scripts/prepare_main_promotion.py"
git config alias.pr-note "!python .scripts/prepare_main_promotion.py --mode pr"
```

Typical founder workflow:

```bash
# while on dev/john or rc/work
git promote-note

# writes .git/PROMOTE_MAIN_MSG.txt with a prefilled summary
# then use that file when merging into main
git checkout main
git merge --no-ff dev/john --file .git/PROMOTE_MAIN_MSG.txt
```

If you prefer not to automate the merge itself, still use the generated note as the canonical description for the promotion.

For external contributors or PR-based work:

```bash
git pr-note

# then paste .git/PULL_REQUEST_BODY.md into the PR description
```

This means the same history-to-summary automation works for both solo-founder promotion into `main` and external contribution review.

---

## Code Conventions

Match existing patterns exactly. Do not introduce new abstractions for one-time use cases.

**Backend (Python / FastAPI)**
- Routers delegate to `api/` sub-modules → services → repositories. Never call the database directly from a router.
- Entity serialization: use the unified `to_dict()` method on all entities. `Collection` is an alias for `Artifact`. Never use legacy `to_dict_workspace()` or `to_dict_collection()`.
- Artifact references are always a single `artifact_id` string. Never add `workspace_id` or `label` to a reference array.
- Agent invocation goes through `POST /artifacts/{id}/invoke`. Do not add new invoke mechanisms or dedicated agent/server endpoints.
- Use `ruff check .` (from `backend/`) before committing.

**Frontend (React / TypeScript)**
- Always use the typed API functions in `frontend/src/api/`. Never call `fetch()` directly — it bypasses auth and error handling.
- Context providers manage state. Add state to the nearest appropriate provider; do not create ad-hoc local fetch loops.
- Use `npm run lint` before committing.

**MCP Servers (servers/)**
- Use `uvicorn.run(mcp.streamable_http_app(), host=MCP_HOST, port=MCP_PORT)`.
- Do not re-implement tools that official vendor MCP servers provide (GitHub, AWS, filesystem, etc.). Register the official server as an `application/vnd.agience.mcp-server+json` artifact instead.

**Documentation**
- New internal docs go in `.dev/`. Public-facing docs go in `.docs/`.
- `.docs/` is an intentional point-in-time public dataset. Duplication with `.dev/` is acceptable when it improves public clarity.
- If a behavior, contract, or user-facing capability changes and that topic exists in both datasets, update both `.docs/` and `.dev/` in tandem.
- Do not document every fix. Write documentation only for new features or major changes.
- Follow the header convention: `Status: **Draft|Reference|Canonical**` and `Date: YYYY-MM-DD`.

---

## Testing

- Bug fixes must include a failing test that reproduces the bug.
- New endpoints require a router test in `backend/tests/test_router_*.py`.
- Non-trivial service logic requires a unit test with mocked dependencies.
- Never hit real ArangoDB, OpenSearch, or S3 in tests.
- Frontend behavior changes require a Vitest + RTL test.

Run the test suite:
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

---

## What Gets Accepted

- Bug fixes with tests
- Documentation corrections and improvements
- New tools for the Agience agent servers (`servers/`) that fit an existing server's domain
- Backend/frontend improvements that follow existing patterns without adding unnecessary abstraction

## What Is Out of Scope

- Docker configuration changes to core services (submit an issue first)
- Re-wrapping vendor APIs that are better served by an official MCP server
- Speculative features without a filed issue and maintainer acknowledgment
- Anything that would add a network call or external dependency without discussion

---

## Pull Request Checklist

Before requesting review:

- [ ] CLA signed (bot will check automatically on PR open)
- [ ] `ruff check .` passes (backend)
- [ ] `pytest tests/` passes (backend)
- [ ] `npm run lint` passes (frontend, if applicable)
- [ ] `npm run test` passes (frontend, if applicable)
- [ ] New behavior is covered by tests
- [ ] No direct database calls from routers
- [ ] No `fetch()` calls in frontend (use `frontend/src/api/`)
- [ ] PR title follows `fix:` / `feat:` / `docs:` / `chore:` convention

---

## Code of Conduct

Be respectful. Contributions, issues, and discussions must remain professional and constructive. Harassment, discrimination, or bad-faith behavior will result in removal from the project.

---

## License

By contributing, you agree that your contributions are licensed to Ikailo Inc. under the [Contributor License Agreement](CLA.md) and may be distributed under the [GNU Affero General Public License v3.0](LICENSE.md), a commercial license, or any other terms at Ikailo Inc.'s discretion.

---

*Ikailo Inc., Canada. Questions: legal@agience.ai*
