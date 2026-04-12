# Content-Type Registry

Status: **Reference**
Date: 2026-04-01

The content-type registry is the indirection layer between the platform's presentation components and the viewer implementations that render specific artifact types. Presentation components never import viewer code directly — they ask the registry to resolve a viewer (and optionally a provider wrapper) for a given MIME type and UX plane.

---

## How the registry works

### Type resolution

When a card is rendered, the platform normalizes the artifact's MIME string and looks it up in the type tree. If an exact match is not found, wildcard inheritance is applied (e.g. `image/webp` falls through to `image/_wildcard`). The result is a `ContentTypeDefinition` carrying the type's label, icon slug, display modes, and declared viewer/provider keys.

### Implementation resolution

Given a `ContentTypeDefinition` and a requested UX plane, `resolveImplementation()` returns the best available implementation:

1. If the type has a `frontend.json` manifest, gather the declared implementations.
2. Filter candidates by MIME match and requested plane.
3. Filter by delivery kind support.
4. Filter by trust policy.
5. Sort by declared priority (higher wins).
6. Select the first compatible candidate.
7. Fall back to the bundled default viewer if nothing qualifies.

### Mounting

`FloatingCardWindow` is the primary mount surface. It calls `resolveImplementation()` and branches on the result's `delivery` field:

- `bundled` — lazy-loads the viewer component from `viewer-map.ts` and renders it directly.
- `web-component` — routes to `McpAppHost`, which renders the viewer inside a sandboxed iframe using the MCP Apps protocol.

```
resolveImplementation(contentType, plane)
          │
          ├─ delivery: "bundled"
          │       └─ viewer-map.ts   →  lazy viewer component
          │
          └─ delivery: "web-component"
                  └─ McpAppHost  →  sandboxed iframe
                                    + MCP Apps protocol
```

---

## How to register a new type

### Step 1: Add the type definition

Create `types/<category>/<subtype>/type.json`:

```json
{
  "content_type": "application/vnd.acme.recipe+json",
  "version": 1,
  "extensions": [".json"],
  "inherits": ["application/json"],
  "description": "Recipe document",
  "ui": {
    "version": 1,
    "label": "Recipe",
    "icon": "chef-hat",
    "color": "#16a34a",
    "modes": ["floating"],
    "states": ["view", "edit"],
    "default_mode": "floating",
    "default_state": "view",
    "is_container": false,
    "creatable": true,
    "viewer": "recipe"
  }
}
```

Display metadata (label, icon, color, viewer key, modes) lives in the `"ui"` key inside `type.json`. There is no separate `presentation.json` file.

If the type lives on a first-party server, place these files under `servers/<name>/ui/<category>/<subtype>/` instead. Server-owned definitions take precedence over the `types/` directory.

### Step 2: Create the viewer component

For bundled delivery, add `frontend/src/content-types/<category>/<subtype>/viewer.tsx`:

```tsx
import type { ViewerProps } from '../../viewer-context';

export function RecipeViewer({ artifact }: ViewerProps) {
  // render artifact.content / artifact.context
}
```

Add `index.ts` in the same directory:

```ts
export const VIEWER_KEY = 'recipe';
export { RecipeViewer as default } from './viewer';
```

### Step 3: Register the viewer

Add an entry to `frontend/src/registry/viewer-map.ts`:

```ts
recipe: () => import('../content-types/application/vnd.acme.recipe+json/viewer'),
```

### Step 4: Register an icon (optional)

Add an entry to `frontend/src/registry/icon-map.ts`:

```ts
'vnd.acme.recipe+json': 'chef-hat',
```

### Step 5: Add a `frontend.json` for multi-plane support (optional)

If the type needs different implementations for different UX surfaces, add `frontend.json` alongside `type.json`:

```json
{
  "version": 1,
  "implementations": [
    {
      "id": "recipe-workspace",
      "planes": ["workspace-window", "workspace-inline"],
      "delivery": "bundled",
      "trust": "first-party",
      "priority": 100,
      "viewer_key": "recipe",
      "provider_key": null,
      "bridge": "agience-card-v1"
    }
  ]
}
```

If `frontend.json` is absent, the resolver uses the `type.json` `ui.viewer` key and the standard bundled path.

---

## Delivery kinds

| Kind | Description |
|---|---|
| `bundled` | Viewer compiled into the host build; loaded via `viewer-map.ts` lazy import |
| `web-component` | Viewer mounted inside a sandboxed iframe; bridge-mediated host access |

### The `agience-card-v1` bridge

Viewers running outside the host React tree (web-component delivery) access platform state through the `agience-card-v1` bridge contract:

| Method | Description |
|---|---|
| `getCard()` | Read the current artifact |
| `updateCard(patch)` | Write a partial update |
| `createChildCard(input)` | Create a child artifact |
| `openCard(cardId)` | Focus a card in the workspace |
| `openCollection(collectionId)` | Navigate to a collection |
| `getSession()` | Returns the active workspace ID |
| `emitTelemetry(event)` | Emit telemetry events |

The bridge is versioned independently from host internals. Type authors target the bridge contract; the platform controls how it is delivered.

---

## UX planes

`resolveImplementation()` accepts a `plane` argument that selects among implementations declared in `frontend.json`:

| Plane | Surface |
|---|---|
| `workspace-window` | Floating card/window in the main product |
| `workspace-inline` | Embedded/expanded artifact inside grids or lists |
| `collection-window` | Committed collection viewer surface |
| `palette-panel` | Command-oriented panel or operator execution surface |
| `chat-embedded` | Inline artifact inside chat or agent transcript |
| `admin-control-plane` | Operator, commercial, and admin views |
| `public-embed` | Constrained unauthenticated or low-trust embed surface |

If no implementation matches the requested plane, the resolver falls back through progressively broader candidates until it reaches the default viewer.

---

## Compatibility rules

The registry is backward-compatible with all existing types:

1. If `frontend.json` is absent, the resolver uses `type.json` `ui.viewer` key — unchanged behavior.
2. If only bundled implementations are declared, behavior is unchanged.
3. If a remote implementation fails to load, the resolver falls back to bundled.
4. If no compatible implementation exists, the platform default viewer is used.

---

## See also

- [Content Types](../architecture/content-types.md) — content-type model, ownership, and trust
- [MCP Overview](../mcp/overview.md) — MCP server and client architecture
- [Layered Architecture](../architecture/layered-architecture.md) — Core / Handler / Presentation boundary rules
