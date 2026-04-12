# Search

Status: **Reference**
Date: 2026-04-01

Agience provides unified hybrid search across all workspaces and collections you have access to. Queries combine keyword (BM25) and semantic (vector) retrieval, with an optional query language for filtering and precision control.

---

## Query syntax

### Terms

| Syntax | Meaning |
|---|---|
| `term` | BM25 lexical match |
| `+term` | Required term |
| `~term` | Semantic term; enables hybrid search |
| `!term` | Excluded term |

### Phrases

| Syntax | Meaning |
|---|---|
| `"phrase"` | Phrase match with stemming |
| `+"phrase"` | Required phrase |
| `="phrase"` | Exact phrase without stemming |
| `~"phrase"` | Semantic phrase; enables hybrid search |
| `!"phrase"` | Excluded phrase |

### Field filters

| Syntax | Meaning |
|---|---|
| `field:value` | Exact keyword match |
| `field:val1,val2` | OR across values |
| `field:>value` | Greater-than range |
| `field:<value` | Less-than range |
| `!field:value` | Exclude value |

### Logic

- Space means OR across unmodified terms
- Comma `,` is explicit OR
- `+` makes a term or phrase required

### Standard fields

The parser recognizes these top-level fields without a `metadata.` prefix:

- `title`
- `description`
- `tags`
- `content`
- `state`
- `type`
- `mime`
- `filename`
- `size`
- `created_at`
- `updated_at`
- `owner_id`
- `collection_id`

Unknown field names are automatically prefixed with `metadata.`.

At least one of the following is required per query:
- Free text terms: `hello world`
- Tags: `tag:budget`
- Filters: `type:pdf size:>1000`

Empty queries are rejected.

---

## Hybrid search

Hybrid search combines BM25 (keyword) and kNN (semantic vector) results via Reciprocal Rank Fusion.

Hybrid mode is enabled when:
1. `@hybrid:on` is present
2. A semantic term `~term` is present
3. A semantic phrase `~"phrase"` is present

Hybrid mode is disabled when:
1. `@hybrid:off` is present
2. The query is filters-only (no free text)
3. All terms are strict required lexical terms with no semantic operator

Aperture filtering (semantic neighborhood control) applies only to the kNN results; BM25 results are never filtered out by aperture.

**Examples:**

| Query | Mode |
|---|---|
| `type:pdf` | BM25 only |
| `+budget +q1 +2025` | BM25 only |
| `+budget ~strategy` | Hybrid |
| `grocery ~store` | Hybrid |
| `~"machine learning"` | Hybrid |

---

## API

### `POST /artifacts/search`

**Request:**

```json
{
  "query_text": "budget ~review type:pdf",
  "scope": ["<collection-or-workspace-id>"],
  "content_types": ["application/pdf"],
  "use_hybrid": true,
  "aperture": 0.75,
  "sort": "relevance",
  "highlight": true,
  "from": 0,
  "size": 20
}
```

| Field | Description |
|---|---|
| `query_text` | Query string using the syntax above |
| `scope` | Optional array of workspace or collection IDs to limit results |
| `content_types` | Optional MIME type filter |
| `use_hybrid` | Force hybrid on/off (overrides query-derived mode) |
| `aperture` | Semantic neighborhood threshold (0–1). Lower = stricter semantic match. |
| `sort` | `"relevance"` (default) or `"recency"` |
| `highlight` | Whether to return highlighted snippets (default `true`) |
| `from` | Pagination offset |
| `size` | Page size |

**Response:**

```json
{
  "hits": [
    {
      "id": "...",
      "score": 1.23,
      "root_id": "...",
      "collection_id": "..."
    }
  ],
  "total": 42,
  "query_text": "budget ~review type:pdf",
  "parsed_query": "+budget type:pdf",
  "corrections": ["+machine learning -> +\"machine learning\""],
  "used_hybrid": true,
  "from": 0,
  "size": 20
}
```

Leaving `scope` empty performs global search across all accessible workspaces and collections.

---

## Further reading

- [Architecture Overview](../architecture/overview.md) — storage and indexing stack
- [Artifact Model](../architecture/artifact-model.md) — artifact fields referenced in search filters
