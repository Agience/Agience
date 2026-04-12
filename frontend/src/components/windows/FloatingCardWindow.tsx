// "Artifact Floating" – full-size movable window for a single artifact on the desktop.
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { X, FolderOpen, Info, Pencil, Tag, Calendar, Folder } from 'lucide-react';
import { IconButton } from '@/components/ui/icon-button';
import { useWorkspace } from '@/hooks/useWorkspace';
import { useWorkspaces } from '@/hooks/useWorkspaces';
import { useCollections } from '@/hooks/useCollections';
import type { Artifact } from '@/context/workspace/workspace.types';
import { safeParseArtifactContext, stringifyArtifactContext } from '@/utils/artifactContext';
import type { ArtifactContext } from '@/utils/artifactContext';
import { getContentType } from '@/registry/content-types';
import type { ContentTypeDefinition } from '@/registry/content-types';
import { defaultFactory } from '@/registry/viewer-map';
import ContainerCardViewer from '@/components/containers/ContainerCardViewer';
import CollectionArtifactViewer from '@/components/collections/CollectionArtifactViewer';
import McpAppHost from '@/isolation/McpAppHost';
import { CollectionChip } from '@/components/common/CollectionChip';
import { CollectionPicker } from '@/components/modals/CollectionPicker';
import { useDebouncedSave } from '@/hooks/useDebouncedSave';
import { addArtifactToCollection, removeArtifactFromCollection } from '@/api/collections';
import { readUiResource } from '@/api/mcp';
import { toast } from 'sonner';
import { COLLECTION_CONTENT_TYPE } from '@/utils/content-type';
import { buildCollectionLabelMap, resolveCollectionLabel } from '@/utils/collectionLabels';

/** Which panel is active in the floating window body */
type ActivePanel = 'content' | 'context' | 'collections';

type Rect = { x: number; y: number; w: number; h: number };

// ─── Context Panel ───────────────────────────────────────────────────────────

function ContextPanel({ artifact }: { artifact: Artifact }) {
	const { artifacts, updateArtifact } = useWorkspace();
	const isReadOnly = useMemo(
		() => !artifacts.find((c) => String(c.id) === String(artifact.id)),
		[artifacts, artifact.id],
	);

	const ctx = useMemo(() => safeParseArtifactContext(artifact.context), [artifact.context]);

	const [editTitle, setEditTitle] = useState(ctx.title || ctx.filename || '');
	const [editDescription, setEditDescription] = useState(ctx.description || '');
	const [editTags, setEditTags] = useState<string[]>(ctx.tags || []);
	const [newTag, setNewTag] = useState('');

	// Build the save payload from current local state
	const contextPayload = useMemo(() => {
		return JSON.stringify({
			title: editTitle,
			description: editDescription,
			tags: editTags,
		});
	}, [editTitle, editDescription, editTags]);

	const { isSaving, lastSaved, resetTracking } = useDebouncedSave(contextPayload, {
		delay: 1500,
		onSave: async () => {
			const updatedContext: ArtifactContext = {
				...ctx,
				title: editTitle,
				description: editDescription,
				tags: editTags,
			};
			await updateArtifact({
				id: String(artifact.id),
				context: stringifyArtifactContext(updatedContext),
			});
		},
		enabled: !isReadOnly,
	});

	// Sync when artifact changes externally
	useEffect(() => {
		const c = safeParseArtifactContext(artifact.context);
		const syncedTitle = c.title || c.filename || '';
		const syncedDescription = c.description || '';
		const syncedTags = c.tags || [];
		setEditTitle(syncedTitle);
		setEditDescription(syncedDescription);
		setEditTags(syncedTags);
		// Mark the synced payload as already-saved so the debounced save
		// doesn't fire a redundant PATCH after an external context update
		// (e.g. header title save completing and pushing a WebSocket event).
		resetTracking(JSON.stringify({
			title: syncedTitle,
			description: syncedDescription,
			tags: syncedTags,
		}));
	}, [artifact.context, resetTracking]);

	const handleAddTag = () => {
		const tag = newTag.trim();
		if (tag && !editTags.includes(tag)) {
			setEditTags([...editTags, tag]);
			setNewTag('');
		}
	};

	const handleRemoveTag = (tag: string) => {
		setEditTags(editTags.filter((t) => t !== tag));
	};

	const timestamp = useMemo(() => {
		const date = new Date(artifact.modified_time || artifact.created_time || 0);
		const now = new Date();
		const diff = now.getTime() - date.getTime();
		const days = Math.floor(diff / (1000 * 60 * 60 * 24));
		if (days === 0) {
			const hours = Math.floor(diff / (1000 * 60 * 60));
			if (hours === 0) {
				const minutes = Math.floor(diff / (1000 * 60));
				return `${minutes} minute${minutes !== 1 ? 's' : ''} ago`;
			}
			return `${hours} hour${hours !== 1 ? 's' : ''} ago`;
		}
		if (days === 1) return 'Yesterday';
		if (days < 7) return `${days} days ago`;
		return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}, [artifact]);

	return (
		<div className="h-full overflow-y-auto p-4 space-y-4">
			{/* Save indicator */}
			{isSaving && (
				<div className="text-xs text-blue-600">Saving...</div>
			)}
			{!isSaving && lastSaved && (
				<div className="text-xs text-green-600">Saved</div>
			)}

			{/* Title */}
			<div>
				<label className="block text-xs font-medium text-gray-500 mb-1">Title</label>
				{isReadOnly ? (
					<div className="text-sm text-gray-900">{editTitle || 'Untitled'}</div>
				) : (
					<input
						type="text"
						value={editTitle}
						onChange={(e) => setEditTitle(e.target.value)}
						className="w-full px-2 py-1.5 text-sm border border-gray-300 rounded focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
						placeholder="Artifact title..."
					/>
				)}
			</div>

			{/* Description */}
			<div>
				<label className="block text-xs font-medium text-gray-500 mb-1">Description</label>
				{isReadOnly ? (
					<div className="text-sm text-gray-700">{editDescription || 'No description'}</div>
				) : (
					<textarea
						value={editDescription}
						onChange={(e) => setEditDescription(e.target.value)}
						className="w-full px-2 py-1.5 text-sm border border-gray-300 rounded resize-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
						rows={3}
						placeholder="Brief description..."
					/>
				)}
			</div>

			{/* Tags */}
			<div>
				<label className="flex items-center gap-1.5 text-xs font-medium text-gray-500 mb-1">
					<Tag className="w-3 h-3" />
					Tags
				</label>
				<div className="flex flex-wrap gap-1 mb-2">
					{editTags.length > 0 ? editTags.map((tag) => (
						<span
							key={tag}
							className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
						>
							#{tag}
							{!isReadOnly && (
								<button
									onClick={() => handleRemoveTag(tag)}
									className="hover:text-blue-900"
								>
									x
								</button>
							)}
						</span>
					)) : (
						<span className="text-xs text-gray-400">No tags</span>
					)}
				</div>
				{!isReadOnly && (
					<div className="flex gap-2">
						<input
							type="text"
							value={newTag}
							onChange={(e) => setNewTag(e.target.value)}
							onKeyDown={(e) => e.key === 'Enter' && handleAddTag()}
							className="flex-1 px-2 py-1 text-xs border border-gray-300 rounded focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
							placeholder="Add tag..."
						/>
						<button
							onClick={handleAddTag}
							className="px-3 py-1 text-xs font-medium text-blue-600 bg-blue-50 rounded hover:bg-blue-100"
						>
							Add
						</button>
					</div>
				)}
			</div>

			{/* Metadata */}
			<div className="space-y-2 pt-2 border-t border-gray-100">
				<div className="flex items-center gap-2 text-xs text-gray-500">
					<Calendar className="w-3.5 h-3.5" />
					<span>{artifact.modified_time ? 'Modified' : 'Created'} {timestamp}</span>
				</div>
				{artifact.state && (
					<div className="flex items-center gap-2 text-xs text-gray-500">
						<span className="font-medium">State:</span>
						<span>{artifact.state}</span>
					</div>
				)}
				{artifact.id && (
					<div className="flex items-center gap-2 text-xs text-gray-500">
						<span className="font-medium">ID:</span>
						<button
							onClick={() => {
								navigator.clipboard.writeText(artifact.id ?? '');
								toast.success('Artifact ID copied');
							}}
							className="font-mono text-xs text-gray-400 hover:text-blue-600 truncate max-w-[200px] cursor-pointer"
							title={artifact.id}
						>
							{artifact.id}
						</button>
					</div>
				)}
				{ctx.content_type && (
					<div className="flex items-center gap-2 text-xs text-gray-500">
						<span className="font-medium">Type:</span>
						<span>{ctx.content_type}</span>
					</div>
				)}
				{ctx.size && (
					<div className="flex items-center gap-2 text-xs text-gray-500">
						<span className="font-medium">Size:</span>
						<span>{(ctx.size / 1024).toFixed(1)} KB</span>
					</div>
				)}
			</div>
		</div>
	);
}

// ─── Collections Panel ───────────────────────────────────────────────────────

function CollectionsPanel({ artifact }: { artifact: Artifact }) {
	const { artifacts, displayedArtifacts = [] } = useWorkspace();
	const { collections = [] } = useCollections();
	const [showPicker, setShowPicker] = useState(false);

	const isReadOnly = useMemo(
		() => !artifacts.find((c) => String(c.id) === String(artifact.id)),
		[artifacts, artifact.id],
	);

	const committedCollectionIds = useMemo(
		() => Array.isArray(artifact.committed_collection_ids)
			? artifact.committed_collection_ids
			: [],
		[artifact.committed_collection_ids],
	);

	const memberships = useMemo(() => {
		const collectionLabelMap = buildCollectionLabelMap([...artifacts, ...displayedArtifacts], collections);

		return committedCollectionIds.map((id) => {
			return { id, name: resolveCollectionLabel(id, collectionLabelMap), status: 'committed' as const };
		});
	}, [committedCollectionIds, collections, artifacts, displayedArtifacts]);

	const handleSelectCollections = async (collectionIds: string[]) => {
		const currentIds = new Set(committedCollectionIds);
		const nextIds = new Set(collectionIds);
		// Add to new collections
		for (const id of nextIds) {
			if (!currentIds.has(id)) {
				await addArtifactToCollection(id, String(artifact.id));
			}
		}
		// Remove from old collections
		for (const id of currentIds) {
			if (!nextIds.has(id)) {
				const rootId = String((artifact as { root_id?: string }).root_id ?? artifact.id);
				await removeArtifactFromCollection(id, rootId);
			}
		}
		setShowPicker(false);
	};

	return (
		<>
			<div className="h-full overflow-y-auto p-4 space-y-3">
				<div className="flex items-center justify-between">
					<label className="flex items-center gap-1.5 text-xs font-medium text-gray-500">
						<Folder className="w-3.5 h-3.5" />
						Collections
					</label>
					{!isReadOnly && (
						<button
							onClick={() => setShowPicker(true)}
							className="text-xs text-blue-600 hover:text-blue-700 font-medium"
						>
							Change
						</button>
					)}
				</div>
				{memberships.length > 0 ? (
					<div className="flex flex-wrap gap-1.5">
						{memberships.map(({ id, name, status }) => (
							<CollectionChip key={id} id={id} name={name} status={status} />
						))}
					</div>
				) : (
					<div className="text-sm text-gray-400">Not in any collections</div>
				)}
			</div>

			<CollectionPicker
				open={showPicker}
				onClose={() => setShowPicker(false)}
				onSelect={handleSelectCollections}
				selectedCollectionIds={committedCollectionIds}
				multiple={true}
				title="Manage Collections"
			/>
		</>
	);
}

// ─── Floating Card Window ────────────────────────────────────────────────────

export default function FloatingCardWindow(props: {
	artifactId: string;
	zIndex: number;
	onClose: () => void;
	onFocus?: () => void;
	onSnapToGrid?: (x: number, y: number) => void;
	initialRect?: Partial<Rect>;
	initialViewState?: string;
	onOpenCollection?: (collectionId: string) => void;
	onOpenArtifact?: (artifact: Artifact) => void;
	windowIndex?: number;
}) {
	const { artifactId, zIndex, onClose, onFocus, onSnapToGrid, initialRect, initialViewState, onOpenCollection, onOpenArtifact, windowIndex = 0 } = props;
	const containerRef = useRef<HTMLDivElement>(null);

	const { artifacts, displayedArtifacts = [], updateArtifact } = useWorkspace();
	const { updateWorkspace, activeWorkspaceId } = useWorkspaces();

	const artifact = useMemo(() => {
		const foundInWorkspace = artifacts.find((c) => String(c.id) === String(artifactId));
		if (foundInWorkspace) return foundInWorkspace;
		return displayedArtifacts.find((c) => String(c.id) === String(artifactId)) || null;
	}, [artifacts, displayedArtifacts, artifactId]);

	const contentType = useMemo(() => {
		if (!artifact) return null;
		return getContentType(artifact);
	}, [artifact]);

	// Does this content type support editing?
	const supportsEdit = useMemo(() => {
		return contentType?.states?.includes('edit') ?? false;
	}, [contentType]);

	// Active panel: content (viewer), context (metadata), collections
	const [activePanel, setActivePanel] = useState<ActivePanel>('content');

	// View state for the content viewer (view or edit)
	const [viewState, setViewState] = useState<string>(() => {
		return initialViewState || contentType?.defaultState || 'view';
	});

	// Reset panel when artifact changes
	useEffect(() => {
		setActivePanel('content');
		setViewState(initialViewState || contentType?.defaultState || 'view');
	}, [artifactId, initialViewState, contentType?.defaultState]);

	// Toggle edit mode
	const toggleEdit = useCallback(() => {
		if (activePanel !== 'content') {
			setActivePanel('content');
			setViewState('edit');
		} else {
			setViewState((prev) => (prev === 'edit' ? 'view' : 'edit'));
		}
	}, [activePanel]);

	// Toggle panel (context or collections) — if already active, flip back to content
	const togglePanel = useCallback((panel: ActivePanel) => {
		setActivePanel((prev) => (prev === panel ? 'content' : panel));
	}, []);

	// MCP Apps: fetch ui:// resource HTML when resourceUri is declared
	const [mcpAppHtml, setMcpAppHtml] = useState<string | null>(null);
	const [mcpAppLoading, setMcpAppLoading] = useState(false);
	const [mcpAppError, setMcpAppError] = useState<string | null>(null);
	// true when failure is a connectivity issue (server down / unreachable)
	const [mcpServerDown, setMcpServerDown] = useState(false);
	const [mcpFetchKey, setMcpFetchKey] = useState(0);
	useEffect(() => {
		if (!contentType?.resourceUri || !contentType?.resourceServer) {
			setMcpAppHtml(null);
			setMcpAppError(null);
			setMcpServerDown(false);
			return;
		}
		let cancelled = false;
		setMcpAppLoading(true);
		setMcpAppError(null);
		setMcpServerDown(false);
		(async () => {
			try {
				const { html } = await readUiResource(
					contentType.resourceServer!,
					contentType.resourceUri!,
					activeWorkspaceId || undefined,
				);
				if (!cancelled) {
					setMcpAppHtml(html || null);
					if (!html) setMcpAppError('Viewer returned empty content');
				}
			} catch (e) {
				console.warn('Failed to load ui:// resource:', e);
				if (!cancelled) {
					setMcpAppHtml(null);
					// Detect server-down: 503/504 from backend proxy, or network error (no response)
					const status = (e as { response?: { status?: number } }).response?.status;
					const isDown = !status || status === 503 || status === 504;
					setMcpServerDown(isDown);
					setMcpAppError(isDown ? null : (e instanceof Error ? e.message : 'Failed to load viewer'));
				}
			} finally {
				if (!cancelled) setMcpAppLoading(false);
			}
		})();
		return () => { cancelled = true; };
	}, [contentType?.resourceUri, contentType?.resourceServer, activeWorkspaceId, mcpFetchKey]);

	// Lazily load the viewer component declared by this content type
	const LazyViewer = useMemo(() => {
		const factory = (contentType as ContentTypeDefinition | null)?.viewer ?? defaultFactory;
		return lazy(factory);
	}, [contentType]);

	const title = useMemo(() => {
		if (!artifact) return '';
		const ctx = safeParseArtifactContext(artifact.context);
		return ctx.title || ctx.filename || artifact.name || '';
	}, [artifact]);

	// Inline-editable title in the window header
	const isArtifactWritable = useMemo(
		() => Boolean(artifacts.find((a) => String(a.id) === String(artifactId))),
		[artifacts, artifactId],
	);
	const [localTitle, setLocalTitle] = useState(title);

	// Sync local title only when the artifact identity changes (different artifact selected),
	// NOT on every context save — syncing on [title] would reset the field mid-typing
	// whenever the PATCH response arrives and updates the store.
	// eslint-disable-next-line react-hooks/exhaustive-deps
	useEffect(() => { setLocalTitle(title); }, [artifactId]);

	const { isSaving: isTitleSaving } = useDebouncedSave(localTitle, {
		delay: 1000,
		enabled: isArtifactWritable,
		onSave: async () => {
			if (!artifact) return;
			const ctx = safeParseArtifactContext(artifact.context);
			await updateArtifact({
				id: String(artifact.id),
				context: stringifyArtifactContext({ ...ctx, title: localTitle }),
			});
			const linkedWorkspaceId =
				(ctx.workspace_id as string | undefined) ||
				(ctx.workspaceId as string | undefined);
			if (linkedWorkspaceId) {
				await updateWorkspace({ id: linkedWorkspaceId, name: localTitle });
			}
		},
	});

	// ─── Drag-and-drop for container viewers ─────────────────────────────────

	const parseDragPayload = useCallback((dt: DataTransfer) => {
		const parseRaw = (raw: string) => {
			if (!raw) return null;
			try {
				return JSON.parse(raw) as {
					ids?: unknown;
					sourceType?: unknown;
					workspaceId?: unknown;
					sourceWorkspaceId?: unknown;
				};
			} catch {
				return null;
			}
		};

		const fromCustom = parseRaw(dt.getData('application/x-agience-artifact'));
		const fromJson = parseRaw(dt.getData('application/json'));
		const payload = fromCustom ?? fromJson;

		if (payload && Array.isArray(payload.ids)) {
			return {
				ids: payload.ids.map(String).filter(Boolean),
				sourceType: typeof payload.sourceType === 'string' ? payload.sourceType : undefined,
				sourceWorkspaceId:
					typeof payload.workspaceId === 'string'
						? payload.workspaceId
						: typeof payload.sourceWorkspaceId === 'string'
							? payload.sourceWorkspaceId
							: undefined,
			};
		}

		const textRaw = dt.getData('text/plain');
		if (textRaw) {
			return { ids: textRaw.split(',').map((value) => value.trim()).filter(Boolean), sourceType: undefined, sourceWorkspaceId: undefined };
		}

		return { ids: [] as string[], sourceType: undefined, sourceWorkspaceId: undefined };
	}, []);

	const handleAssignToCollection = useCallback(async (collectionId: string, dt: DataTransfer) => {
		const { ids } = parseDragPayload(dt);
		if (!ids.length) return;

		for (const draggedId of ids) {
			await addArtifactToCollection(collectionId, draggedId);
		}
	}, [parseDragPayload]);

	// ─── Window drag / resize ────────────────────────────────────────────────

	const [rect, setRect] = useState<Rect>(() => {
		const vw = typeof window !== 'undefined' ? window.innerWidth : 1200;
		const vh = typeof window !== 'undefined' ? window.innerHeight : 800;
		const w = Math.min(initialRect?.w ?? 720, Math.max(420, vw - 160));
		const h = Math.min(initialRect?.h ?? 520, Math.max(320, vh - 200));
		const cx = (vw - w) / 2;
		const cy = (vh - h) / 2;
		const cascade = windowIndex * 30;
		const cascadedX = cx + cascade;
		const cascadedY = cy + cascade;
		const inBounds = cascadedX >= 16 && cascadedX <= vw - w - 16 && cascadedY >= 16 && cascadedY <= vh - h - 16;
		const x = Math.max(16, Math.min(initialRect?.x ?? (inBounds ? cascadedX : cx), vw - w - 16));
		const y = Math.max(16, Math.min(initialRect?.y ?? (inBounds ? cascadedY : cy), vh - h - 16));
		return { x, y, w, h };
	});

	const dragRef = useRef<{ dragging: boolean; dx: number; dy: number; shiftHeld: boolean }>(
		{ dragging: false, dx: 0, dy: 0, shiftHeld: false }
	);

	const clampRect = useCallback((next: Rect): Rect => {
		const vw = window.innerWidth;
		const vh = window.innerHeight;
		const x = Math.max(8, Math.min(next.x, vw - 80));
		const y = Math.max(8, Math.min(next.y, vh - 80));
		const w = Math.max(360, Math.min(next.w, vw - 16));
		const h = Math.max(260, Math.min(next.h, vh - 16));
		return { x, y, w, h };
	}, []);

	const syncRectFromDom = useCallback(() => {
		const el = containerRef.current;
		if (!el) return;
		const r = el.getBoundingClientRect();
		setRect((prev) => {
			const next = clampRect({ ...prev, w: r.width, h: r.height });
			if (next.w === prev.w && next.h === prev.h) return prev;
			return next;
		});
	}, [clampRect]);

	const handleWindowMouseDown = useCallback(() => onFocus?.(), [onFocus]);

	const onHeaderPointerDown = useCallback(
		(e: React.PointerEvent) => {
			onFocus?.();
			if (e.button !== 0) return;
			const el = containerRef.current;
			if (!el) return;

			const r = el.getBoundingClientRect();
			dragRef.current = {
				dragging: true,
				dx: e.clientX - r.left,
				dy: e.clientY - r.top,
				shiftHeld: e.shiftKey,
			};
			(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
			e.preventDefault();
			e.stopPropagation();
		},
		[onFocus]
	);

	const onHeaderPointerMove = useCallback(
		(e: React.PointerEvent) => {
			if (!dragRef.current.dragging) return;
			setRect((prev) => clampRect({
				...prev,
				x: e.clientX - dragRef.current.dx,
				y: e.clientY - dragRef.current.dy,
			}));
			e.preventDefault();
			e.stopPropagation();
		},
		[clampRect]
	);

	const onHeaderPointerUp = useCallback(
		(e: React.PointerEvent) => {
			if (dragRef.current.shiftHeld && onSnapToGrid) {
				const el = containerRef.current;
				if (el) {
					const r = el.getBoundingClientRect();
					onSnapToGrid(r.left + r.width / 2, r.top + r.height / 2);
				}
			}

			dragRef.current.dragging = false;
			syncRectFromDom();
			try {
				(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
			} catch {
				// no-op
			}
			e.preventDefault();
			e.stopPropagation();
		},
		[syncRectFromDom, onSnapToGrid]
	);

	useEffect(() => {
		const onResize = () => setRect((r) => clampRect(r));
		window.addEventListener('resize', onResize);
		return () => window.removeEventListener('resize', onResize);
	}, [clampRect]);

	// ─── Render the content viewer ───────────────────────────────────────────

	const renderContentViewer = () => {
		if (!artifact) return null;

		// MCP Apps iframe path — when a ui:// resource URI is declared
		if (contentType?.resourceUri) {
			if (mcpAppLoading) {
				return (
					<div className="flex items-center justify-center h-full text-sm text-gray-400">
						Loading viewer…
					</div>
				);
			}
			if (mcpServerDown || mcpAppError) {
				const Icon = contentType.icon;
				const ctx = safeParseArtifactContext(artifact.context);
				const artifactTitle = (ctx.title as string | undefined) || (ctx.name as string | undefined);
				return (
					<div className="flex flex-col h-full bg-white overflow-y-auto px-5 py-5">
						<div className="flex items-start gap-3 mb-4">
							<div
								className="flex items-center justify-center w-10 h-10 rounded-lg shrink-0"
								style={{ backgroundColor: contentType.color + '18' }}
							>
								<Icon className="w-5 h-5" style={{ color: contentType.color }} />
							</div>
							<div className="min-w-0">
								<h1 className="text-lg font-semibold text-gray-900 leading-tight">
									{artifactTitle || 'Untitled'}
								</h1>
								<span className="text-xs text-gray-400">{contentType.label}</span>
							</div>
						</div>
						<div className="flex flex-col items-center justify-center flex-1 gap-3 text-center">
							{mcpServerDown ? (
								<>
									<div className="w-8 h-8 rounded-full bg-amber-100 flex items-center justify-center">
										<span className="text-amber-500 text-base">⚠</span>
									</div>
									<p className="text-sm font-medium text-gray-700">Server unavailable</p>
									<p className="text-xs text-gray-400 max-w-[240px]">
										The <span className="font-medium">{contentType.resourceServer}</span> server is not running.
									</p>
								</>
							) : (
								<>
									<p className="text-sm font-medium text-red-500">Viewer unavailable</p>
									<p className="text-xs text-gray-400 max-w-[240px]">{mcpAppError}</p>
								</>
							)}
							<button
								onClick={() => setMcpFetchKey((k) => k + 1)}
								className="mt-1 px-3 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-md transition-colors"
							>
								Retry
							</button>
						</div>
					</div>
				);
			}
			if (mcpAppHtml) {
				return (
					<McpAppHost
						artifact={artifact}
						html={mcpAppHtml}
						resourceServer={contentType?.resourceServer ?? undefined}
						onOpenCollection={onOpenCollection}
						onOpenArtifact={onOpenArtifact}
					/>
				);
			}
		}

		if (contentType?.content_type === COLLECTION_CONTENT_TYPE) {
			return (
				<CollectionArtifactViewer
					artifact={artifact}
					mode={contentType?.defaultMode || 'grid'}
					onOpenArtifact={onOpenArtifact}
				/>
			);
		}

		if (contentType?.isContainer) {
			return (
				<ContainerCardViewer
					artifact={artifact}
					onOpenCollection={onOpenCollection}
					onAssignToCollection={handleAssignToCollection}
				/>
			);
		}

		if (contentType?.viewer) {
			return (
				<Suspense fallback={<div className="flex items-center justify-center h-full text-sm text-gray-400">Loading...</div>}>
					<LazyViewer
						artifact={artifact}
						mode={contentType?.defaultMode || 'floating'}
						state={viewState}
						onOpenCollection={onOpenCollection}
						onOpenArtifact={onOpenArtifact}
					/>
				</Suspense>
			);
		}

		// Fallback: render raw content
		return (
			<div className="p-4 h-full overflow-auto">
				<div className="text-sm font-mono whitespace-pre-wrap">
					{artifact?.content || '(empty)'}
				</div>
			</div>
		);
	};

	return (
		<div
			ref={containerRef}
			className={`fixed bg-white border border-gray-200 rounded-lg shadow-2xl overflow-hidden flex flex-col resize will-change-transform ${
				dragRef.current.shiftHeld && dragRef.current.dragging ? 'ring-2 ring-blue-500' : ''
			}`}
			style={{
				left: 0,
				top: 0,
				transform: `translate(${rect.x}px, ${rect.y}px)`,
				width: rect.w,
				height: rect.h,
				zIndex,
			}}
			onMouseDown={handleWindowMouseDown}
		>
			{/* Header: [Title] ... [Collections] [Context] [Edit?] [Close] */}
			<div
				className="flex items-center justify-between px-3 py-2 border-b bg-white select-none cursor-move"
				onPointerDown={onHeaderPointerDown}
				onPointerMove={onHeaderPointerMove}
				onPointerUp={onHeaderPointerUp}
				onPointerCancel={onHeaderPointerUp}
				title={dragRef.current.shiftHeld ? 'Release to snap to grid' : localTitle}
			>
				<div className="flex-1 min-w-0 pr-2 flex items-center gap-1">
					<span className="text-gray-400 flex-shrink-0" aria-hidden="true">
						<Pencil className="w-3.5 h-3.5" />
					</span>
					{isArtifactWritable ? (
						<input
							type="text"
							value={localTitle}
							onChange={(e) => setLocalTitle(e.target.value)}
							onPointerDown={(e) => e.stopPropagation()}
							className="w-full text-sm font-medium text-gray-800 bg-gray-50/70 border-0 border-b border-gray-200 focus:border-gray-400 focus:outline-none truncate transition-colors hover:border-gray-300 rounded-sm px-1 py-0.5"
							placeholder="Untitled"
							aria-label="Card title"
						/>
					) : (
						<div className="text-sm font-medium text-gray-800 truncate">{localTitle}</div>
					)}
					{isTitleSaving && <span className="text-[10px] text-blue-400 flex-shrink-0">•</span>}
					{dragRef.current.shiftHeld && dragRef.current.dragging && (
						<span className="ml-2 text-xs text-blue-600 flex-shrink-0">(shift to snap)</span>
					)}
				</div>
				<div className="flex items-center gap-1">
					{/* Collections */}
					<IconButton
						size="sm"
						variant="ghost"
						active={activePanel === 'collections'}
						onPointerDown={(e) => e.stopPropagation()}
						onClick={(e) => {
							e.preventDefault();
							e.stopPropagation();
							togglePanel('collections');
						}}
						title="Collections"
					>
						<FolderOpen />
					</IconButton>

					{/* Context */}
					<IconButton
						size="sm"
						variant="ghost"
						active={activePanel === 'context'}
						onPointerDown={(e) => e.stopPropagation()}
						onClick={(e) => {
							e.preventDefault();
							e.stopPropagation();
							togglePanel('context');
						}}
						title="Context"
					>
						<Info />
					</IconButton>

					{/* Edit — only if content type supports it */}
					{supportsEdit && (
						<IconButton
							size="sm"
							variant="ghost"
							active={viewState === 'edit' && activePanel === 'content'}
							onPointerDown={(e) => e.stopPropagation()}
							onClick={(e) => {
								e.preventDefault();
								e.stopPropagation();
								toggleEdit();
							}}
							title="Edit"
						>
							<Pencil />
						</IconButton>
					)}

					{/* Close */}
					<IconButton
						size="sm"
						variant="ghost"
						onPointerDown={(e) => e.stopPropagation()}
						onClick={(e) => {
							e.preventDefault();
							e.stopPropagation();
							onClose();
						}}
						title="Close"
					>
						<X />
					</IconButton>
				</div>
			</div>

			{/* Body */}
			<div className="flex-1 overflow-hidden">
				{activePanel === 'context' && artifact ? (
					<ContextPanel artifact={artifact} />
				) : activePanel === 'collections' && artifact ? (
					<CollectionsPanel artifact={artifact} />
				) : (
					renderContentViewer()
				)}
			</div>
		</div>
	);
}
