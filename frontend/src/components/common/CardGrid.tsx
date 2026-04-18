import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { Artifact } from '../../context/workspace/workspace.types';
import { CardGridItem } from './CardGridItem';
import { useWorkspace } from '../../context/workspace/WorkspaceContext';
import { getStableArtifactId } from '@/utils/artifact-identifiers';

type DropPosition = 'before' | 'after' | null;

const DND_MIME = 'application/x-agience-artifact';

const isArtifactsDrag = (e: React.DragEvent) =>
  Array.from(e.dataTransfer?.types ?? []).includes(DND_MIME);

const isFileDrag = (e: React.DragEvent) => {
  const t = new Set(Array.from(e.dataTransfer?.types ?? []));
  return t.has('Files') || t.has('public.file-url');
};

const compareByOrderKeyStable = (a: Artifact, b: Artifact) => {
  const ka = (a.order_key || '');
  const kb = (b.order_key || '');
  const primary = ka.localeCompare(kb);
  if (primary !== 0) return primary;

  const ta = new Date(a.created_time || 0).getTime();
  const tb = new Date(b.created_time || 0).getTime();
  if (ta !== tb) return ta - tb;

  return String(a.id || '').localeCompare(String(b.id || ''));
};

function getArtifactIdForGrid(
  artifact: Artifact,
  preferRootId = false,
): string | null {
  if (preferRootId && artifact.root_id != null) {
    const root = String(artifact.root_id).trim();
    if (root) return root;
  }
  return getStableArtifactId(artifact);
}

interface CardGridProps {
  artifacts: Artifact[];
  artifactCountsById?: Record<string, number>;
  selectedIds?: string[];
  selectable?: boolean;
  draggable?: boolean;
  editable?: boolean;
  inPanel?: boolean;
  fillHeight?: boolean; // NEW: control whether grid fills available height
  isSelected?: (id: string) => boolean;
  activeSource?: { type: string; id: string } | null;
  isShowingSearchResults?: boolean;
  onArtifactMouseDown?: (id: string, e: React.MouseEvent) => void;
  onOpenArtifact?: (artifact: Artifact) => void;
  onRemove?: (artifact: Artifact) => void;
  onRevert?: (artifact: Artifact) => void;
  onAddToWorkspace?: (artifact: Artifact) => void;
  onAssignCollections?: (artifact: Artifact) => void;
  onOrder?: (orderedIds: string[]) => void;
  onEditArtifactOpen?: () => void;
  onEditArtifactClose?: () => void;
  onFileDrop?: (insertIndex: number, files: File[]) => void;
  onArtifactDrop?: (
    insertIndex: number,
    draggedIds: string[],
    dragPayload?: {
      sourceType?: string;
      sourceId?: string;
      workspaceId?: string;
      collectionIds?: string[];
      rootIds?: string[];
      versionIds?: string[];
    },
  ) => void;
  externalTailHover?: 'file' | 'artifacts' | null;
}

export default function CardGrid({
  artifacts,
  artifactCountsById = {},
  selectedIds = [],
  selectable = false,
  draggable = false,
  editable = true,
  inPanel = false,
  fillHeight = false, // NEW: default to natural height
  isSelected,
  activeSource,
  isShowingSearchResults = false,
  onArtifactMouseDown,
  onOpenArtifact,
  onRemove,
  onRevert,
  onAddToWorkspace,
  onAssignCollections,
  onOrder,
  onFileDrop,
  onEditArtifactOpen,
  onArtifactDrop,
}: CardGridProps) {
  const { updateArtifact, unselectAllArtifacts } = useWorkspace();
  const allowFileDrop = activeSource?.type === 'workspace';

  const getId = useCallback(
    (artifact: Artifact) => getArtifactIdForGrid(artifact, isShowingSearchResults),
    [isShowingSearchResults]
  );

  // local ordering
  const [orderedIds, setOrderedIds] = useState<string[]>(() => {
    if (!artifacts.length) return [];
    const sorted = [...artifacts].sort(compareByOrderKeyStable);
    return sorted
      .map((artifact) => getArtifactIdForGrid(artifact, isShowingSearchResults))
      .filter((id): id is string => Boolean(id));
  });

  // Sync orderedIds when artifacts prop changes - add/remove IDs without disrupting order
  useEffect(() => {
    const currentIds = new Set(orderedIds);
    const propsIds = artifacts
      .map((artifact) => getId(artifact))
      .filter((id): id is string => Boolean(id));
    const propsIdSet = new Set(propsIds);
    
    // Remove IDs that no longer exist
    const filtered = orderedIds.filter(id => propsIdSet.has(id));
    
    // Add new IDs in order_key sorted position
    const newIds = propsIds.filter(id => !currentIds.has(id));
    if (newIds.length > 0) {
      // Insert each new ID at the position its order_key earns it, without
      // disturbing the drag-applied local order of existing IDs.
      const sortedArtifacts = [...artifacts].sort(compareByOrderKeyStable);
      const sortedIds = sortedArtifacts
        .map((artifact) => getId(artifact))
        .filter((id): id is string => Boolean(id));

      let result = [...filtered];
      for (const newId of newIds) {
        // Find where this ID sits in the server-sorted order relative to its neighbors
        const serverIdx = sortedIds.indexOf(newId);
        // Look for the nearest existing ID that appears before newId in server order
        let insertAt = result.length; // default: append
        for (let si = serverIdx - 1; si >= 0; si--) {
          const candidateId = sortedIds[si];
          const localIdx = result.indexOf(candidateId);
          if (localIdx !== -1) {
            insertAt = localIdx + 1;
            break;
          }
        }
        result = [...result.slice(0, insertAt), newId, ...result.slice(insertAt)];
      }
      setOrderedIds(result);
    } else if (filtered.length !== orderedIds.length) {
      setOrderedIds(filtered);
    }
  }, [artifacts, orderedIds, getId]);

  const idToArtifact = useMemo(() => {
    const m = new Map<string, Artifact>();
    artifacts.forEach((c) => {
      const key = getId(c);
      if (key) m.set(key, c);
    });
    return m;
  }, [artifacts, getId]);

  const orderedArtifacts = useMemo(
    () => orderedIds.map((id) => idToArtifact.get(id)).filter(Boolean) as Artifact[],
    [orderedIds, idToArtifact]
  );

  // When double-clicking an artifact, just select it and notify parent to ensure preview pane is visible
  const handleEdit = (artifact: Artifact) => {
    // Prevent edit if a delete just happened (to avoid accidental double-click)
    if (preventEditRef.current) return;
    onEditArtifactOpen?.(); // Notify parent (e.g., to show preview pane if hidden)
    // Single-select this artifact - parent will show it in the inline editor
    const stableId = getId(artifact);
    if (stableId) {
      onArtifactMouseDown?.(stableId, { button: 0 } as React.MouseEvent);
    }
  };

  // Track which artifact index should be force-hovered after a delete
  const [forceHoverIndex, setForceHoverIndex] = useState<number | null>(null);
  const preventEditRef = useRef(false);
  const forceHoverTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearForceHoverTimeout = useCallback(() => {
    if (forceHoverTimeoutRef.current) {
      clearTimeout(forceHoverTimeoutRef.current);
      forceHoverTimeoutRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      clearForceHoverTimeout();
    };
  }, [clearForceHoverTimeout]);

  const handleDeleteViaRemove = async (artifact: Artifact) => {
    const stableId = getId(artifact);
    const index = stableId ? orderedArtifacts.findIndex(c => getId(c) === stableId) : -1;
    if (index !== -1) {
      preventEditRef.current = true;
      setForceHoverIndex(index);
      // Clear after a longer delay to ensure the new artifact appears and receives hover state
      clearForceHoverTimeout();
      forceHoverTimeoutRef.current = setTimeout(() => {
        setForceHoverIndex(null);
        preventEditRef.current = false;
      }, 300);
    }
    onRemove?.(artifact);
  };
  const handleRemove = (artifact: Artifact) => {
    const stableId = getId(artifact);
    const index = stableId ? orderedArtifacts.findIndex(c => getId(c) === stableId) : -1;
    if (index !== -1) {
      preventEditRef.current = true;
      setForceHoverIndex(index);
      clearForceHoverTimeout();
      forceHoverTimeoutRef.current = setTimeout(() => {
        setForceHoverIndex(null);
        preventEditRef.current = false;
      }, 300);
    }
    onRemove?.(artifact);
  };
  const handleRevert = (artifact: Artifact) => onRevert?.(artifact);

  // DnD state
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [hoverPos, setHoverPos] = useState<DropPosition>(null);
  const [fileHoverIndex, setFileHoverIndex] = useState<number | null>(null);
  const [fileHoverPos, setFileHoverPos] = useState<DropPosition>(null);
  const itemRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  // Add ref for external tail hover positioning
  const gridRef = useRef<HTMLDivElement>(null);

  const clearHover = useCallback(() => {
    setHoverIndex(null);
    setHoverPos(null);
    setFileHoverIndex(null);
    setFileHoverPos(null);
  }, []);

  // Memoized function to calculate indicator position based on actual gaps
  const getIndicatorPosition = useCallback((artifactId: string, index: number, pos: 'before' | 'after') => {
    const artifactEl = itemRefs.current.get(artifactId);
    if (!artifactEl) return null;

    // Prefer the inner preview element for accurate sizing; fall back to wrapper
    const artifactPreview = (artifactEl.querySelector('[style*="width: 180px"]') as HTMLElement) ?? artifactEl;

    const artifactRect = artifactPreview.getBoundingClientRect();
    const gap = 12; // Grid gap-3 = 12px
    const spacing = gap / 2; // Indicator offset: half the gap (6px from artifact edge)
    
    // Helper to find all artifacts in the same row and get max height
    const getRowHeight = (refRect: DOMRect): number => {
      let maxHeight = refRect.height;
      let minTop = refRect.top;

      // Check all artifacts for same row
      orderedArtifacts.forEach(artifact => {
        const key = getId(artifact);
        if (!key) return;
        const el = itemRefs.current.get(key);
        if (el) {
          const preview = (el.querySelector('[style*="width: 180px"]') as HTMLElement) ?? el;
          const rect = preview.getBoundingClientRect();
          // Same row if within 8px vertical tolerance
          if (Math.abs(rect.top - refRect.top) < 8) {
            maxHeight = Math.max(maxHeight, rect.height);
            minTop = Math.min(minTop, rect.top);
          }
        }
      });

      return maxHeight;
    };

    if (pos === 'before') {
      // Check if there's an artifact before this one in the same row
      if (index > 0) {
        const prevArtifact = orderedArtifacts[index - 1];
        const prevKey = getId(prevArtifact);
        const prevEl = prevKey ? itemRefs.current.get(prevKey) : undefined;
        if (prevEl) {
          const prevPreview = (prevEl.querySelector('[style*="width: 180px"]') as HTMLElement) ?? prevEl;
          const prevRect = prevPreview.getBoundingClientRect();
          // Check if prev artifact is in the same row (within 8px vertical tolerance)
          const sameRow = Math.abs(prevRect.top - artifactRect.top) < 8;
          if (sameRow) {
            // Position indicator at exact midpoint of the gap
            const rowHeight = getRowHeight(artifactRect);
            const minTop = Math.min(prevRect.top, artifactRect.top);
            // Always use spacing offset from artifact edge for consistency
            const indicatorLeft = prevRect.right + spacing;
            return { left: indicatorLeft, top: minTop, height: rowHeight };
          }
        }
      }
      // First in row - align to left edge with spacing
      const rowHeight = getRowHeight(artifactRect);
      return { left: artifactRect.left - spacing, top: artifactRect.top, height: rowHeight };
    } else {
      // pos === 'after'
      if (index < orderedArtifacts.length - 1) {
        const nextArtifact = orderedArtifacts[index + 1];
        const nextKey = getId(nextArtifact);
        const nextEl = nextKey ? itemRefs.current.get(nextKey) : undefined;
        if (nextEl) {
          const nextPreview = (nextEl.querySelector('[style*="width: 180px"]') as HTMLElement) ?? nextEl;
          const nextRect = nextPreview.getBoundingClientRect();
          // Check if next artifact is in the same row (within 8px vertical tolerance)
          const sameRow = Math.abs(nextRect.top - artifactRect.top) < 8;
          if (sameRow) {
            // Position indicator at exact midpoint of the gap
            const rowHeight = getRowHeight(artifactRect);
            const minTop = Math.min(artifactRect.top, nextRect.top);
            // Always use spacing offset from artifact edge for consistency
            const indicatorLeft = artifactRect.right + spacing;
            return { left: indicatorLeft, top: minTop, height: rowHeight };
          }
        }
      }
      // Last in row - align to right edge with spacing
      const rowHeight = getRowHeight(artifactRect);
      return { left: artifactRect.right + spacing, top: artifactRect.top, height: rowHeight };
    }
  }, [orderedArtifacts, getId]);

  // ensure guides clear on dragend only - don't clear on every drop
  useEffect(() => {
    const clear = () => clearHover();
    window.addEventListener('dragend', clear);
    return () => {
      window.removeEventListener('dragend', clear);
    };
  }, [clearHover]);

  const parseDragIds = (e: React.DragEvent): string[] | null => {
    try {
      const raw = e.dataTransfer.getData(DND_MIME);
      if (raw) {
        const p = JSON.parse(raw);
        if (p && Array.isArray(p.ids)) return p.ids.map(String);
      }
    } catch {
      // ignore
    }
    try {
      const txt = e.dataTransfer.getData('application/json') || e.dataTransfer.getData('text/plain');
      if (!txt) return null;
      const maybe = JSON.parse(txt);
      if (Array.isArray(maybe?.ids)) return maybe.ids.map(String);
      if (Array.isArray(maybe)) return maybe.map(String);
      if (typeof maybe === 'string') return maybe.split(',').map((s) => s.trim());
    } catch {
      const txt = e.dataTransfer.getData('text/plain');
      if (txt) return txt.split(',').map((s) => s.trim());
    }
    return null;
  };

  const parseDragPayloadMeta = (e: React.DragEvent) => {
    const parseRaw = (raw: string) => {
      if (!raw) return null;
      try {
        return JSON.parse(raw) as {
          sourceType?: unknown;
          sourceId?: unknown;
          workspaceId?: unknown;
          collectionIds?: unknown;
          rootIds?: unknown;
          versionIds?: unknown;
        };
      } catch {
        return null;
      }
    };

    const payload = parseRaw(e.dataTransfer.getData(DND_MIME))
      ?? parseRaw(e.dataTransfer.getData('application/json'));

    if (!payload) return undefined;

    return {
      sourceType: typeof payload.sourceType === 'string' ? payload.sourceType : undefined,
      sourceId: typeof payload.sourceId === 'string' ? payload.sourceId : undefined,
      workspaceId: typeof payload.workspaceId === 'string' ? payload.workspaceId : undefined,
      collectionIds: Array.isArray(payload.collectionIds) ? payload.collectionIds.map(String).filter(Boolean) : undefined,
      rootIds: Array.isArray(payload.rootIds) ? payload.rootIds.map(String).filter(Boolean) : undefined,
      versionIds: Array.isArray(payload.versionIds) ? payload.versionIds.map(String).filter(Boolean) : undefined,
    };
  };

  const isArtifactsDragLike = (e: React.DragEvent): boolean => {
    if (isFileDrag(e) && !isArtifactsDrag(e)) return false;
    if (isArtifactsDrag(e)) return true;
    const ids = parseDragIds(e);
    return !!(ids && ids.length);
  };

  const handleDragOverArtifact = (e: React.DragEvent, targetId: string, index: number) => {
    // Get the CardPreview element (child of the wrapper) for accurate positioning;
    // fall back to the wrapper itself if the inner element isn't mounted yet.
    const wrapperEl = itemRefs.current.get(targetId);
    if (!wrapperEl) return;
    const artifactEl = (wrapperEl.querySelector('[style*="width: 180px"]') as HTMLElement) ?? wrapperEl;

    const rect = artifactEl.getBoundingClientRect();

    // prevent Workspace handler from also seeing this dragover
    e.stopPropagation();

    if (allowFileDrop && isFileDrag(e) && !isArtifactsDragLike(e)) {
      e.preventDefault();
      try { if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'; } catch { 
        // ignore
      }
      const x = e.clientX - rect.left;
      setFileHoverIndex(index);
      setFileHoverPos(x < rect.width / 2 ? 'before' : 'after');
      return;
    }

    if (!draggable) return;

    if (isArtifactsDragLike(e)) {
      e.preventDefault();
      try { if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'; } catch { 
        // ignore
      }
      const x = e.clientX - rect.left;
      setHoverIndex(index);
      setHoverPos(x < rect.width / 2 ? 'before' : 'after');
    }
  };

  const applyOrder = (
    dragIds: string[],
    targetId: string,
    pos: DropPosition,
    dragPayload?: {
      sourceType?: string;
      sourceId?: string;
      workspaceId?: string;
      collectionIds?: string[];
      versionIds?: string[];
    },
  ) => {
    if (!pos) return;

    const idsFromProps = artifacts
      .map((artifact) => getId(artifact))
      .filter((id): id is string => Boolean(id));
    const present = new Set(idsFromProps);
    const notPresent = dragIds.filter(id => !present.has(id));
    const filteredDrag = dragIds.filter((id) => present.has(id));
    
    // NEW: If artifacts are external, notify parent FIRST, then return (parent will update and we'll re-render)
    if (notPresent.length > 0 && onArtifactDrop) {
      const insertAtBase = orderedIds.indexOf(targetId);
      const insertAt = pos === 'before' ? insertAtBase : insertAtBase + 1;
      console.log('[CardGrid] Calling onArtifactDrop with insertIndex:', insertAt, 'external IDs:', notPresent);
      onArtifactDrop(insertAt, notPresent, dragPayload);
      clearHover();
      return; // Don't reorder yet - wait for parent to update artifacts prop
    }
    
    // Only reorder artifacts that are already in our list
    if (!filteredDrag.length) return;
    if (filteredDrag.includes(targetId)) return;

    const current = [...orderedIds];
    const dragSet = new Set(filteredDrag);

    // Separate the dragged artifacts from the rest, preserving order
    const remaining = current.filter((id) => !dragSet.has(id));
    const draggedOrdered = current.filter((id) => dragSet.has(id)); // Preserves original order

    const insertAtBase = remaining.indexOf(targetId);
    if (insertAtBase === -1) return;
    const insertAt = pos === 'before' ? insertAtBase : insertAtBase + 1;

    // Insert the group of dragged artifacts at the target position
    const next = [...remaining.slice(0, insertAt), ...draggedOrdered, ...remaining.slice(insertAt)];
    setOrderedIds(next);
    onOrder?.(next);
    clearHover();
  };

  const handleDropOnArtifact = (e: React.DragEvent, targetId: string, index: number) => {
    e.stopPropagation();
    e.preventDefault();

    if (isFileDrag(e) && !isArtifactsDragLike(e)) {
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length && onFileDrop) {
        // Determine insert position: use hover state if it matched this card,
        // otherwise derive position from cursor vs card center so drop always works
        // even when the spatial guard in handleGridDragOver didn't set hover state.
        let pos: DropPosition;
        if (fileHoverIndex === index && fileHoverPos) {
          pos = fileHoverPos;
        } else {
          const wrapperEl = itemRefs.current.get(targetId);
          const artifactEl = wrapperEl?.querySelector('[style*="width: 180px"]') as HTMLElement | null;
          if (artifactEl) {
            const rect = artifactEl.getBoundingClientRect();
            pos = e.clientX - rect.left < rect.width / 2 ? 'before' : 'after';
          } else {
            pos = 'after';
          }
        }
        const insertAt = pos === 'before' ? index : index + 1;
        clearHover();
        onFileDrop(insertAt, files);
      } else {
        clearHover();
      }
      return;
    }

    if (!draggable) {
      clearHover();
      return;
    }

    if (isArtifactsDragLike(e)) {
      const dragged = parseDragIds(e);
      const dragPayload = parseDragPayloadMeta(e);
      if (dragged?.length) {
        applyOrder(dragged, targetId, hoverPos, dragPayload);
      } else {
        clearHover();
      }
    } else {
      clearHover();
    }
  };

  

  const handleGridDragLeave = (e: React.DragEvent) => {
    // clear when leaving the grid entirely
    const curr = e.currentTarget as HTMLElement;
    const rel = e.relatedTarget as Node | null;
    if (!rel || !curr.contains(rel)) clearHover();
  };

  // Accept drags on empty grid space
  const handleGridDragOver = (e: React.DragEvent) => {
    e.stopPropagation();
    
    // Always preventDefault for file drags to prevent browser navigation
    if (isFileDrag(e)) {
      e.preventDefault();
    }

    // Check if we're actually in empty space (after all artifacts) or just in a gap between rows
    const allArtifactRects: DOMRect[] = [];
    orderedArtifacts.forEach(artifact => {
      const key = getId(artifact);
      if (!key) return;
      const el = itemRefs.current.get(key);
      if (el) {
        const artifactEl = (el.querySelector('[style*="width: 180px"]') as HTMLElement) ?? el;
        allArtifactRects.push(artifactEl.getBoundingClientRect());
      }
    });

    // If we have artifacts, check if mouse is in truly empty space
    if (allArtifactRects.length > 0) {
      const maxBottom = Math.max(...allArtifactRects.map(r => r.bottom));
      const mouseY = e.clientY;
      const mouseX = e.clientX;

      // Find the rightmost artifact in the bottom row
      const bottomRowArtifacts = allArtifactRects.filter(r => Math.abs(r.bottom - maxBottom) < 8);
      const maxRight = bottomRowArtifacts.length > 0
        ? Math.max(...bottomRowArtifacts.map(r => r.right))
        : 0;

      // Only show end-of-list if:
      // - Mouse is below all artifacts (below maxBottom + tolerance), OR
      // - Mouse is in the bottom row but to the right of the rightmost artifact
      const isBelowAllArtifacts = mouseY > maxBottom + 20;
      const isRightOfLastRow = mouseY >= maxBottom - 50 && mouseY <= maxBottom + 20 && mouseX > maxRight + 20;

      if (!isBelowAllArtifacts && !isRightOfLastRow) {
        // We're in a gap between artifacts/rows, not truly at the end.
        // Accept artifact drops (no end-of-list indicator) so cross-panel drags
        // (e.g. from the search panel) can reach the workspace CardGrid.
        if (draggable && isArtifactsDragLike(e)) {
          e.preventDefault();
          try { if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'; } catch { /* ignore */ }
        }
        return;
      }
    }

    // Truly in empty space after all artifacts (or completely empty grid)
    if (allowFileDrop && isFileDrag(e) && !isArtifactsDragLike(e)) {
      try { if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'; } catch {
        // ignore
       }
      setFileHoverIndex(orderedArtifacts.length);
      setFileHoverPos('after');
      return;
    }
    if (draggable && isArtifactsDragLike(e)) {
      e.preventDefault();
      try { if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'; } catch {
        // ignore
      }
      setHoverIndex(orderedArtifacts.length);
      setHoverPos('after');
    }
  };

  const handleGridDrop = (e: React.DragEvent) => {
    e.stopPropagation();
    e.preventDefault();

    if (allowFileDrop && isFileDrag(e) && !isArtifactsDragLike(e)) {
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length && onFileDrop) {
        // Append to end when dropping files on empty space (same as artifact drops)
        clearHover();
        onFileDrop(orderedArtifacts.length, files);
      } else {
        clearHover();
      }
      return;
    }

    if (draggable && isArtifactsDragLike(e)) {
      const dragged = parseDragIds(e);
      const dragPayload = parseDragPayloadMeta(e);
      if (dragged && dragged.length) {
        const idsFromProps = artifacts
          .map((artifact) => getId(artifact))
          .filter((id): id is string => Boolean(id));
        const present = new Set(idsFromProps);
        const notPresent = dragged.filter(id => !present.has(id));
        
        console.log('[CardGrid] handleGridDrop - dragged:', dragged);
        console.log('[CardGrid] handleGridDrop - notPresent:', notPresent);
        
        // NEW: If artifacts are external, notify parent and return
        if (notPresent.length > 0 && onArtifactDrop) {
          console.log('[CardGrid] Calling onArtifactDrop for grid drop (append)');
          onArtifactDrop(orderedArtifacts.length, notPresent, dragPayload);
          clearHover();
          return; // Don't try to reorder external artifacts
        }
        
        // Only reorder artifacts that are already in the grid
        const current = [...orderedIds];
        const dragSet = new Set(dragged);
        const remaining = current.filter(id => !dragSet.has(id));
        const draggedOrdered = current.filter(id => dragSet.has(id));
        const next = [...remaining, ...draggedOrdered];
        setOrderedIds(next);
        onOrder?.(next);
      }
      clearHover();
    }
  };

  // Background click anywhere in the grid should deselect all
  const handleGridClick = (e: React.MouseEvent) => {
    // Check if the click was on an artifact - if so, don't deselect
    const target = e.target as Element;
    const clickedOnArtifact = target.closest('[style*="width: 180px"]');

    if (!clickedOnArtifact) {
      unselectAllArtifacts();
    }
  };

  const gridStyle = useMemo(() => ({ gridAutoRows: 'min-content' as const }), []);

  return (
    <>
      <div
        ref={gridRef}
        data-role="artifact-grid"
        className={`
           grid gap-3
           grid-cols-[repeat(auto-fill,180px)]
           justify-start content-start
           [&::-webkit-scrollbar-thumb]:bg-gray-400
           [&::-webkit-scrollbar-track]:bg-transparent
           [&::-webkit-scrollbar]:w-1
           relative
           ${fillHeight ? 'min-h-full' : ''}
        `}
        style={gridStyle}
        onDragOver={handleGridDragOver}
        onDrop={handleGridDrop}
        onDragLeave={handleGridDragLeave}
        onClick={handleGridClick}
      >
        {orderedArtifacts.map((artifact, index) => {
          const id = getId(artifact);
          if (!id) return null;

          const payload = draggable
            ? {
                type: 'artifacts' as const,
                ids: selectedIds.includes(id) ? selectedIds : [id],
                rootIds: orderedArtifacts
                  .filter((candidate) => {
                    const candidateId = getId(candidate);
                    return candidateId ? (selectedIds.includes(id) ? selectedIds.includes(candidateId) : candidateId === id) : false;
                  })
                  .map((candidate) => String(candidate.root_id ?? candidate.id ?? ''))
                  .filter(Boolean),
                versionIds: orderedArtifacts
                  .filter((candidate) => {
                    const candidateId = getId(candidate);
                    return candidateId ? (selectedIds.includes(id) ? selectedIds.includes(candidateId) : candidateId === id) : false;
                  })
                  .map((candidate) => String(candidate.id ?? ''))
                  .filter(Boolean),
              }
            : undefined;

          const isHover = hoverIndex === index && hoverPos !== null;
          const isFileHoverHere = fileHoverIndex === index && fileHoverPos !== null;

          // Use memoized function - only recalculate when hover state changes
          const hoverIndicatorPos = isHover && hoverPos ? getIndicatorPosition(id, index, hoverPos) : null;
          const fileIndicatorPos = isFileHoverHere && fileHoverPos ? getIndicatorPosition(id, index, fileHoverPos) : null;

          return (
            <div
              key={id}
              ref={(el) => { if (el) itemRefs.current.set(id, el); else itemRefs.current.delete(id); }}
              onDragOver={(e) => handleDragOverArtifact(e, id, index)}
              onDrop={(e) => handleDropOnArtifact(e, id, index)}
              className="w-full relative flex justify-start"
            >
              <div className="relative">
                {/* Drop indicator for artifact drags - positioned at gap midpoint or edge */}
                {hoverIndicatorPos && (
                  <div
                    className="pointer-events-none fixed w-0.5 bg-blue-500 z-[1000]"
                    style={{
                      left: hoverIndicatorPos.left,
                      top: hoverIndicatorPos.top,
                      height: hoverIndicatorPos.height,
                    }}
                  />
                )}

                {/* Drop indicator for file drags - positioned at gap midpoint or edge */}
                {allowFileDrop && fileIndicatorPos && (
                  <div
                    className="pointer-events-none fixed w-0.5 bg-blue-500 z-[1000]"
                    style={{
                      left: fileIndicatorPos.left,
                      top: fileIndicatorPos.top,
                      height: fileIndicatorPos.height,
                    }}
                  />
                )}

                <CardGridItem
                  artifact={artifact}
                  artifactCount={artifactCountsById[String(artifact.id)] ?? 0}
                  selectable={selectable}
                  editable={editable}
                  draggable={draggable}
                  inPanel={inPanel}
                  isSelected={isSelected?.(id) ?? false}
                  forceHover={forceHoverIndex === index}
                  activeSource={activeSource ?? undefined}
                  isShowingSearchResults={isShowingSearchResults}
                  onMouseDown={(e) => onArtifactMouseDown?.(id, e)}
                  onEdit={(c) => handleEdit(c)}
                  onOpen={onOpenArtifact}
                  onRemove={(c) => (c.state === 'draft' ? handleDeleteViaRemove(c) : handleRemove(c))}
                  onRevert={handleRevert}
                  onArchive={!inPanel ? async (c) => {
                    if (!c.id) return;
                    await updateArtifact({ id: String(c.id), state: 'archived' });
                  } : undefined}
                  onRestore={async (c) => {
                    if (!c.id) return;
                    await updateArtifact({ id: String(c.id), state: 'committed' });
                  }}
                  onAddToWorkspace={onAddToWorkspace}
                  onAssignCollections={onAssignCollections}
                  dragData={payload}
                />
              </div>
            </div>
          );
        })}
      </div>

      {/* External end-of-list indicator - positioned next to last artifact using fixed positioning */}
      {(
        (fileHoverIndex === orderedArtifacts.length && fileHoverPos === 'after') ||
        (hoverIndex === orderedArtifacts.length && hoverPos === 'after')
      ) && orderedArtifacts.length > 0 && (() => {
        const lastArtifact = orderedArtifacts[orderedArtifacts.length - 1];
        const lastArtifactKey = getId(lastArtifact);
        if (!lastArtifactKey) return null;
        const lastArtifactEl = itemRefs.current.get(lastArtifactKey);
        if (!lastArtifactEl) return null;

        const artifactPreview = (lastArtifactEl.querySelector('[style*="width: 180px"]') as HTMLElement) ?? lastArtifactEl;

        const artifactRect = artifactPreview.getBoundingClientRect();
        const gap = 12; // Grid gap-3 = 12px
        const spacing = gap / 2; // 4px from artifact edge (consistent with between-artifact indicators)

        return (
          <div
            className="pointer-events-none fixed w-0.5 bg-blue-500 z-[1000]"
            style={{
              top: artifactRect.top,
              left: artifactRect.right + spacing,
              height: artifactRect.height,
            }}
          />
        );
      })()}
    </>
  );
}