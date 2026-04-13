// src/components/common/CardGridItem.tsx
// "Artifact Preview" – the small grid tile representation of a single artifact inside the Artifact Grid.
import { MouseEvent, DragEvent, useState, useRef, useEffect, useMemo } from 'react';
import clsx from 'clsx';
import {
  FiTrash2 as Trash,            // Delete (draft, last collection)
  FiX as XIcon,                  // Remove from collection / panel
  FiFolder as FolderIcon,        // Collection assignment
} from 'react-icons/fi';
import { Artifact } from '../../context/workspace/workspace.types';
import { getStableArtifactId } from '@/utils/artifact-identifiers';
import { invokeArtifact } from '@/api/artifacts';
import { toast } from 'sonner';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from '../ui/context-menu';
import { Badge } from '../ui/badge';
import { IconButton } from '../ui/icon-button';
import { TYPE_COLORS } from '@/utils/type-colors';
import { getArtifactType, getArtifactIcon, getArtifactColor, isArtifactActionBlocked, shouldShowStateBadge, getStateBadgeLabel } from '@/utils/artifact-visual-helpers';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { useCollections } from '../../context/collections/CollectionsContext';
import { getContentType } from '@/registry/content-types';
import { ContainerTreePreview } from '../containers/ContainerCardViewer';
import { CardContextItems } from './CardContextItems';
import type { CardActionId } from './CardContextItems';
import { useWorkspace } from '@/hooks/useWorkspace';
import { buildCollectionLabelMap, resolveCollectionLabel } from '@/utils/collectionLabels';
type GridDragData = { ids?: string[]; rootIds?: string[]; versionIds?: string[] } | undefined;

function getDragStableId(
  artifact: Artifact,
  isShowingSearchResults = false,
): string | null {
  if (isShowingSearchResults && artifact.root_id != null) {
    const root = String(artifact.root_id).trim();
    if (root) return root;
  }

  return getStableArtifactId(artifact);
}

interface CardGridItemProps {
  artifact: Artifact;
  artifactCount?: number;
  onMouseDown?: (e: MouseEvent | DragEvent) => void;
  onEdit?: (artifact: Artifact) => void;
  onOpen?: (artifact: Artifact) => void;

  onRemove?: (artifact: Artifact) => void;
  onRevert?: (artifact: Artifact) => void;
  onArchive?: (artifact: Artifact) => void;
  onRestore?: (artifact: Artifact) => void;
  onAddToWorkspace?: (artifact: Artifact) => void;
  onAssignCollections?: (artifact: Artifact) => void;

  draggable?: boolean;
  selectable?: boolean;
  isSelected?: boolean;
  dragData?: GridDragData;
  editable?: boolean;
  inPanel?: boolean;
  forceHover?: boolean;
  activeSource?: { type?: 'workspace' | 'collection' | 'mcp-server'; id?: string };
  isShowingSearchResults?: boolean;
}

function parseContextTitle(context: unknown): string {
  try {
    if (typeof context === 'string') {
      const obj = JSON.parse(context);
      if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
        const contextObj = obj as { filename?: string; title?: string };
        const f = contextObj.filename;
        if (typeof f === 'string' && f.trim()) return f.trim();
        const t = contextObj.title;
        if (typeof t === 'string' && t.trim()) return t.trim();
      }
      return '';
    }
    if (context && typeof context === 'object' && !Array.isArray(context)) {
      const f = (context as { filename?: string }).filename;
      if (typeof f === 'string' && f.trim()) return f.trim();
      const t = (context as { title?: string }).title;
      return typeof t === 'string' ? t.trim() : '';
    }
  } catch {
    // Ignore parsing errors
  }
  return '';
}

interface ContextMeta {
  content_type?: string;
  bytes?: number;
  upload?: { status?: string; progress?: number };
  processing?: { strategy?: string; status?: string; handler?: string | null };
  [key: string]: unknown;
}

function readMeta(context: unknown): { mime?: string; bytes?: number; upload?: { status?: string; progress?: number }; processing?: { strategy?: string; status?: string; handler?: string | null } } {
  try {
    const obj = typeof context === 'string' ? JSON.parse(context) : context;
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      const metaObj = obj as ContextMeta;
      const mime = metaObj.content_type;
      const bytes = metaObj.bytes;
      const upload = metaObj.upload && typeof metaObj.upload === 'object' ? metaObj.upload : undefined;
      const processing = metaObj.processing && typeof metaObj.processing === 'object' ? metaObj.processing : undefined;
      return {
        mime: typeof mime === 'string' ? mime : undefined,
        bytes: typeof bytes === 'number' ? bytes : undefined,
        upload: upload as { status?: string; progress?: number } | undefined,
        processing: processing as { strategy?: string; status?: string; handler?: string | null } | undefined,
      };
    }
  } catch {
    // Ignore parsing errors
  }
  return {};
}

type UploadMeta = { status?: string; progress?: number };
type CtxMeta = {
  filename?: string;
  content_type?: string;
  bytes?: number;
  upload?: UploadMeta;
};

function readUpload(context: unknown): UploadMeta | undefined {
  try {
    const obj = typeof context === 'string' ? JSON.parse(context) : context;
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      const up = (obj as CtxMeta).upload;
      if (up && typeof up === 'object') return up;
    }
  } catch {
    // ignore
  }
  return undefined;
}

export const CardGridItem = ({
  artifact,
  artifactCount = 0,
  onMouseDown,
  onOpen,
  onEdit,
  onRemove,
  onRevert,
  onArchive,
  onRestore,
  onAddToWorkspace,
  onAssignCollections,
  draggable = false,
  selectable = false,
  isSelected = false,
  dragData,
  editable = true,
  inPanel = false,
  forceHover = false,
  activeSource,
  isShowingSearchResults = false,
}: CardGridItemProps) => {
  const [hovered, setHovered] = useState(forceHover);
  const draggingRef = useRef(false);
  const { collections } = useCollections();
  const { artifacts, displayedArtifacts = [] } = useWorkspace();

  // Artifact type visual system
  const artifactType = getArtifactType(artifact);
  const CardIcon = getArtifactIcon(artifactType);
  const artifactBarColor = getArtifactColor(artifactType);

  const meta = useMemo(() => readMeta(artifact.context), [artifact.context]);

  const contentType = useMemo(() => getContentType(artifact), [artifact]);
  const removeLabel = activeSource?.type === 'collection' ? 'Remove from collection' : 'Remove from workspace';

  // Drop zone — any artifact can opt in via context.drop.enabled
  const dropConfig = useMemo(() => {
    try {
      const ctx = typeof artifact.context === 'string'
        ? JSON.parse(artifact.context)
        : artifact.context;
      if (ctx?.drop?.enabled) {
        return ctx.drop as { enabled: boolean; label?: string; accepts?: string[] };
      }
    } catch { /* ignore */ }
    return null;
  }, [artifact.context]);
  const [isDropTarget, setIsDropTarget] = useState(false);
  const collectionLabelMap = useMemo(
    () => buildCollectionLabelMap([...artifacts, ...displayedArtifacts], collections),
    [artifacts, displayedArtifacts, collections],
  );
  const showContainerTreePreview =
    contentType.isContainer && Boolean(contentType.containerVariant);

  // If forceHover becomes true, set hovered to true
  useEffect(() => {
    if (forceHover) {
      setHovered(true);
    }
  }, [forceHover]);

  const collectionIds = useMemo(() => {
    if (Array.isArray(artifact.committed_collection_ids)) return artifact.committed_collection_ids;
    return [] as string[];
  }, [artifact.committed_collection_ids]);

  const classes = clsx(
    'relative rounded border shadow-sm p-4 bg-white',
    'transition-all duration-200 ease-out',
    'hover:shadow-md',
    'group',
    contentType.tileClassName,
    // State-specific border and background
    artifact.state === 'archived'
      ? 'bg-gray-100 opacity-60 border-gray-300'
      : 'border-gray-200',
    selectable && 'cursor-pointer hover:ring-1 hover:ring-purple-300',
    isSelected && 'ring-2 ring-purple-600 border-purple-600',
    isDropTarget && 'ring-2 ring-violet-500 shadow-lg shadow-violet-200/50 border-violet-400'
  );

  const handleMouseDown = () => {
    draggingRef.current = false;
  };

  const handleMouseUp = (e: MouseEvent) => {
    // Only trigger selection if we didn't drag
    if (!draggingRef.current && onMouseDown) {
      onMouseDown(e);
    }
  };

  const handleDragStart = (e: DragEvent) => {
    draggingRef.current = true;

    // If this artifact wasn't selected, select it before starting the drag
    if (!isSelected && onMouseDown) onMouseDown(e as unknown as MouseEvent);

    const stableId = getDragStableId(artifact, isShowingSearchResults);
    if (!stableId) {
      e.preventDefault();
      return;
    }

    // If we have a selection, drag them all; otherwise just this artifact
    const ids =
      Array.isArray(dragData?.ids) && dragData.ids.length > 1 && isSelected
        ? dragData.ids
        : [stableId];
    const rootIds =
      Array.isArray(dragData?.rootIds) && dragData.rootIds.length > 0
        ? dragData.rootIds
        : [String(artifact.root_id ?? artifact.id ?? '')].filter(Boolean);
    const versionIds =
      Array.isArray(dragData?.versionIds) && dragData.versionIds.length > 0
        ? dragData.versionIds
        : [String(artifact.id ?? '')].filter(Boolean);

    // Include workspace and collection context for sidebar drop handling
    const payload = { 
      type: 'artifacts' as const, 
      ids,
      rootIds,
      versionIds,
      workspaceId: artifact.collection_id,
      collectionIds: collectionIds || [],
      sourceType: activeSource?.type || (artifact.collection_id ? 'workspace' : 'collection'),
      sourceId: activeSource?.id,
    };

    // Primary custom type CardGrid expects
    e.dataTransfer.setData('application/x-agience-artifact', JSON.stringify(payload));

    // Fallbacks for environments that strip custom types
    e.dataTransfer.setData('application/json', JSON.stringify(payload));
    e.dataTransfer.setData('text/plain', ids.join(','));

    // Signal intention
    e.dataTransfer.effectAllowed = 'move';
  };

  const handleDragEnd = () => {
    draggingRef.current = false;
  };

  // ── Drop zone handlers (driven by context.drop) ──────────────────────
  const handleDropDragOver = (e: DragEvent) => {
    if (!dropConfig) return;
    if (!e.dataTransfer.types.includes('application/x-agience-artifact')) return;
    e.stopPropagation();
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    setIsDropTarget(true);
  };

  const handleDropDragLeave = (e: DragEvent) => {
    if (!dropConfig) return;
    const el = e.currentTarget as HTMLElement;
    const rel = e.relatedTarget as Node | null;
    if (!rel || !el.contains(rel)) {
      setIsDropTarget(false);
    }
  };

  const mimeMatches = (artifactMime: string | undefined, accepts: string[]): boolean => {
    if (!artifactMime) return true; // unknown type — let the server decide
    const norm = artifactMime.split(';')[0]?.trim().toLowerCase() ?? '';
    return accepts.some(pattern => {
      if (pattern === '*/*') return true;
      if (pattern.endsWith('/*')) return norm.startsWith(pattern.slice(0, -1));
      return norm === pattern.toLowerCase();
    });
  };

  const handleDropOnCard = async (e: DragEvent) => {
    if (!dropConfig) return;
    e.stopPropagation();
    e.preventDefault();
    setIsDropTarget(false);

    const raw = e.dataTransfer.getData('application/x-agience-artifact');
    if (!raw) return;
    let ids: string[] = [];
    try {
      const payload = JSON.parse(raw);
      if (Array.isArray(payload.ids)) ids = payload.ids.map(String);
    } catch { return; }

    ids = ids.filter(id => id !== String(artifact.id));
    if (!ids.length) return;

    // Filter by accepted MIME types if configured
    if (dropConfig.accepts?.length) {
      const allArtifacts = [...(artifacts || []), ...(displayedArtifacts || [])];
      ids = ids.filter(id => {
        const a = allArtifacts.find(art => String(art.id) === id || String(art.root_id) === id);
        return mimeMatches(a?.content_type, dropConfig.accepts!);
      });
      if (!ids.length) {
        toast.warning(`This transform only accepts: ${dropConfig.accepts.join(', ')}`);
        return;
      }
    }

    const artifactId = String(artifact.id);
    const workspaceId = activeSource?.type === 'workspace' ? activeSource.id : undefined;
    const label = dropConfig.label ?? 'Running transform';

    toast.info(`${label}…`);
    try {
      const result = await invokeArtifact(artifactId, undefined, undefined, workspaceId, ids);
      if (result.error) {
        toast.error(`Transform failed: ${result.error}`);
      } else {
        toast.success('Transform completed');
      }
    } catch (err) {
      toast.error(`Transform failed: ${err instanceof Error ? err.message : 'Unknown error'}`);
    }
  };

  const heading =
    parseContextTitle(artifact.context) ||
    (typeof artifact.name === 'string' ? artifact.name.trim() : '') ||
    (typeof artifact.description === 'string' ? artifact.description.trim() : '');
  const upload = readUpload(artifact.context);
  const contentText = (() => {
    if (artifact.content == null) return '';
    const raw = typeof artifact.content === 'string' ? artifact.content : JSON.stringify(artifact.content);
    // Strip common markdown syntax for a clean preview
    return raw
      .replace(/^#{1,6}\s+/gm, '')      // headings
      .replace(/\*\*(.+?)\*\*/g, '$1')   // bold
      .replace(/\*(.+?)\*/g, '$1')        // italic
      .replace(/__(.+?)__/g, '$1')         // bold alt
      .replace(/_(.+?)_/g, '$1')           // italic alt
      .replace(/~~(.+?)~~/g, '$1')        // strikethrough
      .replace(/`{3}[\s\S]*?`{3}/g, '')  // code blocks
      .replace(/`(.+?)`/g, '$1')           // inline code
      .replace(/^>\s+/gm, '')             // blockquotes
      .replace(/^[-*+]\s+/gm, '')         // unordered lists
      .replace(/^\d+\.\s+/gm, '')         // ordered lists
      .replace(/!?\[([^\]]+)\]\([^)]+\)/g, '$1') // links/images
      .replace(/^[-*_]{3,}$/gm, '')       // hr
      .trim();
  })();

  // Helper function for state badge with better styling
  // state badge unified to purple styling — function removed for consistency

  // Map CardContextItems action IDs to prop callbacks
  const handleAction = (actionId: CardActionId | string, actionArtifact: Artifact) => {
    switch (actionId) {
      case 'open':
        if (onOpen) { onOpen(actionArtifact); } else { onEdit?.(actionArtifact); }
        break;
      case 'edit':
        onEdit?.(actionArtifact);
        break;
      case 'delete':
      case 'remove':
        onRemove?.(actionArtifact);
        break;
      case 'archive':
        onArchive?.(actionArtifact);
        break;
      case 'restore':
        onRestore?.(actionArtifact);
        break;
      case 'revert':
        onRevert?.(actionArtifact);
        break;
      case 'add-to-workspace':
        onAddToWorkspace?.(actionArtifact);
        break;
      case 'assign-collections':
        onAssignCollections?.(actionArtifact);
        break;
      default:
        break;
    }
  };

  const renderActions = () => {
    if (!hovered) return null;

    // In panel mode - show X to remove from panel
    if (inPanel) {
      if (!onRemove) return null;
      return (
        <div className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
          <IconButton
            size="xs"
            variant="ghost"
            onMouseDown={(e) => e.stopPropagation()}
            onMouseUp={(e) => e.stopPropagation()}
            onClick={(e) => { e.stopPropagation(); onRemove(artifact); }}
            title="Remove from panel"
          >
            <XIcon />
          </IconButton>
        </div>
      );
    }

    // Single hover button — always X (remove from collection).
    // Exception: draft with no committed version and no other collections → Trash (delete).
    // See .dev/features/card-actions.md for full matrix.
    // Archive, Revert, Restore live in the context menu only.
    // Drafts are always removable (allows cancelling uploads).
    if (!onRemove) return null;
    if (artifact.state !== 'draft' && isArtifactActionBlocked(artifact, 'remove')) return null;

    const currentContainerId = activeSource?.id;
    const otherCollections = (collectionIds || []).filter(id => id !== currentContainerId);
    const isRealDelete =
      artifact.state === 'draft' &&
      !artifact.has_committed_version &&
      otherCollections.length === 0;

    return (
      <div className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
        <IconButton
          size="xs"
          variant="ghost"
          onMouseDown={(e) => e.stopPropagation()}
          onMouseUp={(e) => e.stopPropagation()}
          onClick={(e) => { e.stopPropagation(); onRemove(artifact); }}
          title={isRealDelete ? 'Delete' : removeLabel}
        >
          {isRealDelete ? <Trash /> : <XIcon />}
        </IconButton>
      </div>
    );
  };

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          className={classes}
          style={{ width: '180px' }}
          data-testid={`artifact-${artifact.id}`}
          data-state={artifact.state}
          onMouseDown={handleMouseDown}
          onMouseUp={handleMouseUp}
          onDoubleClick={(e) => {
            if (editable === false || artifact.state === 'archived') return;
            e.stopPropagation();
            if (onOpen) {
              onOpen(artifact);
              return;
            }
            onEdit?.(artifact);
          }}
          draggable={draggable}
          onDragStart={draggable ? handleDragStart : undefined}
          onDragEnd={draggable ? handleDragEnd : undefined}
          onDragOver={dropConfig ? handleDropDragOver : undefined}
          onDragLeave={dropConfig ? handleDropDragLeave : undefined}
          onDrop={dropConfig ? handleDropOnCard : undefined}
          title={isDropTarget ? (dropConfig?.label ?? 'Drop to run') : undefined}
          onMouseEnter={() => setHovered(true)}
          onMouseLeave={() => setHovered(false)}
        >
      {/* upload progress bar */}
      {meta.upload?.status && meta.upload.status !== 'complete' && typeof meta.upload.progress === 'number' && (
        <div className="absolute left-0 right-0 bottom-0 h-1 bg-gray-200">
          <div
            className="h-1 bg-blue-500 transition-all"
            style={{ width: `${Math.max(0, Math.min(100, Math.round(meta.upload.progress * 100)))}%` }}
            title={`Uploading ${Math.round((meta.upload.progress || 0) * 100)}%`}
          />
        </div>
      )}

      {/* processing status indicator */}
      {meta.processing?.status === 'pending_handler' && (
        <div className="absolute left-0 right-0 bottom-0 h-1 bg-amber-300" title="Awaiting processing handler" />
      )}
      {meta.upload?.status === 'failed' && (
        <div className="absolute left-0 right-0 bottom-0 h-1 bg-red-500" title="Upload failed" />
      )}

      {renderActions()}

      {/* Title: max 2 lines with type icon */}
      {heading && (
        <div className="flex items-start justify-between gap-2 mb-2">
          <div className="flex items-start gap-1.5 flex-1 min-w-0">
            <CardIcon className="flex-shrink-0 mt-0.5" size={14} style={{ color: artifactBarColor }} />
            <div className="min-w-0 flex-1">
              <h3 className="font-semibold text-gray-900 text-sm line-clamp-2" title={heading}>
                {heading}
              </h3>
              {artifactCount > 0 && (
                <div className="mt-1">
                  <Badge className="border-0 bg-slate-100 text-slate-700 text-[10px] px-1.5 py-0.5 font-medium">
                    {artifactCount} artifact{artifactCount === 1 ? '' : 's'}
                  </Badge>
                </div>
              )}
            </div>
          </div>
          {contentType.badgeClassName && (
            <Badge
              className={clsx(
                'border-0 text-[10px] px-1.5 py-0.5 font-medium shrink-0',
                contentType.badgeClassName
              )}
              title={contentType.label}
            >
              {contentType.label}
            </Badge>
          )}
        </div>
      )}

      {/* Type color horizontal divider between header and content */}
      <div
        className="w-full h-0.5 rounded mb-2"
        style={{ backgroundColor: artifactBarColor }}
      />

      {/* Content: container artifacts show tree preview; others show text snippet */}
      {!upload?.status || upload.status === 'complete' ? (
        showContainerTreePreview ? (
          <div className="mt-1 text-xs">
            <ContainerTreePreview artifact={artifact} />
          </div>
        ) : (
          <p className="text-gray-600 text-xs mt-1 line-clamp-3">
            {contentText}
          </p>
        )
      ) : null}

      {/* Footer: date and state badge */}
      <div className="flex items-center justify-between text-xs text-gray-500 mt-3 pt-2 border-t border-gray-100">
        <span>{artifact.created_time ? new Date(artifact.created_time).toLocaleDateString() : ''}</span>
        <div className="flex gap-1.5 items-center">
          {/* Show collection badge if artifact belongs to collections */}
          {collectionIds.length > 0 && (
            <Popover>
              <PopoverTrigger asChild>
                <Badge
                  className={clsx(
                    'bg-gradient-to-br from-blue-400/20 via-cyan-400/20 to-teal-400/20 hover:from-blue-500/30 hover:via-cyan-500/30 hover:to-teal-500/30',
                    'border border-blue-200/40 text-blue-700',
                    'text-[10px] px-1.5 py-0.5 font-medium flex items-center gap-1 cursor-pointer'
                  )}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (onAssignCollections) {
                      onAssignCollections(artifact);
                    }
                  }}
                  title="Click to manage collections"
                >
                  <FolderIcon size={10} />
                  {collectionIds.length}
                </Badge>
              </PopoverTrigger>
              <PopoverContent side="top" className="w-auto p-3">
                <div className="text-xs">
                  <ul className="space-y-1">
                    {collectionIds.map(collectionId => {
                      return (
                        <li key={collectionId} className="text-gray-700">
                          <div className="flex items-center justify-between gap-3">
                            <span className="inline-flex items-center gap-1">
                              <span
                                className="inline-block h-1.5 w-1.5 rounded-full"
                                style={{ backgroundColor: TYPE_COLORS.resources.solid500 }}
                              />
                              {resolveCollectionLabel(collectionId, collectionLabelMap)}
                            </span>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              </PopoverContent>
            </Popover>
          )}
          {/* Workspace state badge (only show new, modified, archived) */}
          {shouldShowStateBadge(artifact.state) && (
            <Badge className="bg-gradient-to-br from-purple-400/25 via-pink-400/25 to-blue-400/25 border border-purple-200/50 text-purple-700 text-[10px] px-1.5 py-0.5 font-medium">
              {getStateBadgeLabel(artifact.state)}
            </Badge>
          )}
        </div>
      </div>

      {/* Progress bar - only while uploading */}
      {upload?.status && upload.status !== 'complete' && typeof upload.progress === 'number' && (
        <div className="mt-2">
          <div className="h-1 w-full bg-gray-200 rounded">
            <div
              className="h-1 bg-blue-500 rounded transition-all"
              style={{ width: `${Math.max(0, Math.min(100, Math.round((upload.progress || 0) * 100)))}%` }}
              title={`Uploading ${Math.round((upload.progress || 0) * 100)}%`}
            />
          </div>
          <div className="text-[10px] text-gray-500 mt-1">{Math.round((upload.progress || 0) * 100)}%</div>
        </div>
      )}
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className="w-56">
        <CardContextItems
          artifact={artifact}
          contentType={contentType}
          onAction={handleAction}
          isSearchResult={isShowingSearchResults}
          removeLabel={removeLabel}
        />
      </ContextMenuContent>
    </ContextMenu>
  );
};