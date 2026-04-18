# agience-server-atlas

Status: **Reference** --- current server.py surface
Date: 2026-03-31

Atlas is the provenance and governance persona. It traces lineage, detects conflicts, and validates contracts.

## Current MCP Tools

Implemented paths:

| Tool | Description |
|---|---|
| `check_provenance` | Trace artifact lineage and source attribution |
| `detect_conflicts` | Find conflicting claims in a workspace or collection |
| `apply_contract` | Validate workspace artifacts against a contract artifact |

Declared placeholders:

| Tool | Description |
|---|---|
| `suggest_merge` | Propose a merge of divergent artifact versions |
| `traverse_graph` | Follow relationship edges in the knowledge graph |
| `attribute_source` | Attach source attribution metadata |
| `check_coherence` | Assess logical coherence across a set of artifacts |

## Configuration

- `AGIENCE_API_URI`
- `AGIENCE_API_KEY`
- `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT`, `LOG_LEVEL`

## Running

```bash
pip install -r requirements.txt
python server.py
```

Several implemented Atlas tools currently delegate into existing backend agents through `POST /artifacts/{id}/invoke`; keep the README aligned to the live `server.py` surface, not the legacy source history.
