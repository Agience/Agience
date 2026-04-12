import { useState, useMemo, useCallback, useEffect } from 'react';
import MDEditor from '@uiw/react-md-editor';
import { Tag, Calendar, Folder, Trash2, Archive, RotateCcw, RefreshCw, FolderInput, Save, X, Pencil, Wand2 } from 'lucide-react';
import { useWorkspace } from '../../hooks/useWorkspace';
import { useCollections } from '../../hooks/useCollections';
import { addArtifactToCollection, removeArtifactFromCollection } from '../../api/collections';
import { useShortcuts } from '../../context/shortcuts/useShortcuts';
import { useDebouncedSave } from '../../hooks/useDebouncedSave';
import { useArtifactContent } from '../../hooks/useArtifactContent';
import { CollectionPicker } from '../modals/CollectionPicker';
import { CollectionChip } from '../common/CollectionChip';
import { ContentRenderer } from './ContentRenderer';
import { EditorFactory } from './editors';
import { useConfirm } from '../../context/dialog/useConfirm';
import { CARD_CONFIRM, BUTTON_LABELS } from '@/constants/strings';
import { safeParseArtifactContext, stringifyArtifactContext, type ArtifactContext } from '@/utils/artifactContext';
import { getTransformFromArtifact } from '@/context/palette/orderSpec';
import TransformCardSummary from '@/components/palette/OrderCardSummary';
// type color accents are applied on grid artifacts; preview keeps neutral header per latest guidance

interface CardEditorProps {
  artifactId: string;
  onClose?: () => void;
}

/**
 * CardEditor - Unified view/edit component for artifacts
 * 
 * Replaces the separate preview + modal edit pattern with inline editing.
 * Markdown content can be edited directly with live preview.
 * Context fields (title, tags) and collections are editable inline.
 */
export function CardEditor({ artifactId, onClose }: CardEditorProps) {
  const {
    artifacts,
    displayedArtifacts = [],
    removeArtifact,
    revertArtifact,
    updateArtifact,
    extractInformationFromSelection,
  } = useWorkspace();
  const { collections = [] } = useCollections();
  const dangerConfirm = useConfirm();
  const { registerShortcut } = useShortcuts();
  
  const [isEditing, setIsEditing] = useState(false);
  const [isEditingContent, setIsEditingContent] = useState(false);
  const [showCollectionPicker, setShowCollectionPicker] = useState(false);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);

  // Find artifact
  const artifact = useMemo(() => {
    const foundInWorkspace = artifacts.find(c => String(c.id) === artifactId);
    if (foundInWorkspace) return foundInWorkspace;
    return displayedArtifacts.find(c => String(c.id) === artifactId) || null;
  }, [artifacts, displayedArtifacts, artifactId]);

  // Determine if this is a collection artifact (read-only)
  const isCollectionArtifact = useMemo(() => {
    return !artifacts.find(c => String(c.id) === artifactId);
  }, [artifacts, artifactId]);

  // Parse context
  const ctx: ArtifactContext = useMemo(() => {
    if (!artifact) return {} as ArtifactContext;
    return safeParseArtifactContext(artifact.context);
  }, [artifact]);

  const transformArtifact = useMemo(() => {
    if (!artifact) return null;
    return getTransformFromArtifact(artifact);
  }, [artifact]);

  // Resolve content — fetches from S3 if artifact.content is empty and content_key is set.
  const { content: resolvedContent } = useArtifactContent(artifact ?? { id: '', content: '', context: '' } as Parameters<typeof useArtifactContent>[0]);

  // Local edit state
  const [editContent, setEditContent] = useState(resolvedContent);
  const [editTitle, setEditTitle] = useState(ctx.title || ctx.filename || '');
  const [editTags, setEditTags] = useState<string[]>(ctx.tags || []);
  const [editDescription, setEditDescription] = useState(ctx.description || '');
  const [newTag, setNewTag] = useState('');

  // Keep editContent in sync when resolvedContent arrives asynchronously (S3 fetch).
  useEffect(() => {
    if (resolvedContent && editContent === '') {
      setEditContent(resolvedContent);
    }
  // Only run when resolvedContent changes; do not overwrite user edits.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedContent]);

  // Handlers - defined before early return to satisfy React Hooks rules
  const handleSave = useCallback(async () => {
    if (!artifact) return;
    const updatedContext: ArtifactContext = {
      ...ctx,
      title: editTitle || ctx.title,
      description: editDescription || ctx.description,
      tags: editTags,
    };

    await updateArtifact({
      id: String(artifact.id),
      content: editContent,
      context: stringifyArtifactContext(updatedContext),
    });

    setHasUnsavedChanges(false);
    setIsEditing(false);
  }, [artifact, editContent, editTitle, editDescription, editTags, ctx, updateArtifact]);

  const handleCancel = useCallback(() => {
    if (!artifact) return;
    // Reset to original values
    setEditContent(resolvedContent);
    setEditTitle(ctx.title || ctx.filename || '');
    setEditTags(ctx.tags || []);
    setEditDescription(ctx.description || '');
    setHasUnsavedChanges(false);
    setIsEditing(false);
  }, [artifact, ctx, resolvedContent]);

  // Auto-save content changes
  const { isSaving: isContentSaving, lastSaved: contentLastSaved, forceSave: forceSaveContent } = useDebouncedSave(
    editContent,
    {
      delay: 2000,
      onSave: async (newContent) => {
        if (!artifact) return;
        await updateArtifact({
          id: String(artifact.id),
          content: newContent,
        });
      },
      enabled: isEditingContent && editContent !== artifact?.content,
    }
  );

  // Register keyboard shortcuts for content editing mode
  useEffect(() => {
    if (!isEditingContent) return;

    const cleanupEscape = registerShortcut({
      id: 'artifact-editor-exit-content-edit',
      label: 'Exit content edit mode',
      group: 'Artifact Editor',
      combos: ['Escape'],
      handler: () => {
        setIsEditingContent(false);
        setEditContent(artifact?.content || '');
      },
      options: {
        description: 'Cancel inline content editing',
        allowInInputs: true,
      },
    });

    const cleanupSave = registerShortcut({
      id: 'artifact-editor-force-save',
      label: 'Force save changes',
      group: 'Artifact Editor',
      combos: ['mod+s'],
      handler: (e) => {
        e.preventDefault();
        forceSaveContent();
      },
      options: {
        description: 'Immediately save content changes',
        allowInInputs: true,
      },
    });

    return () => {
      cleanupEscape();
      cleanupSave();
    };
  }, [isEditingContent, registerShortcut, forceSaveContent, artifact]);

  // Format timestamp
  const timestamp = useMemo(() => {
    if (!artifact) return '';
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

  const handleExtractUnits = useCallback(async () => {
    if (!artifact) return;
    if (isCollectionArtifact) return;
    const sourceId = String(artifact.id ?? '');
    if (!sourceId) return;
    await extractInformationFromSelection({ sourceArtifactId: sourceId });
  }, [artifact, isCollectionArtifact, extractInformationFromSelection]);

  // Check if content is markdown-editable
  const isMarkdownContent = useMemo(() => {
    if (!artifact) return false;
    if (transformArtifact) return false;
    const mime = ctx.content_type || '';
    return (
      !mime ||
      mime.startsWith('text/') ||
      mime === 'application/json' ||
      ctx.content_source !== 'agience-content' // Only text content, not binary files
    );
  }, [artifact, ctx, transformArtifact]);

  // Early return after all hooks
  if (!artifact) {
    return (
      <div className="flex flex-col items-center justify-center h-full px-8 text-center">
        <X className="w-16 h-16 text-gray-300 mb-4" />
        <h3 className="text-lg font-semibold text-gray-700 mb-2">Artifact not found</h3>
        <p className="text-sm text-gray-500">
          The selected artifact may have been deleted or moved
        </p>
      </div>
    );
  }

  const title = editTitle || ctx.title || ctx.filename || 'Untitled Artifact';

  const committedCollectionIds = Array.isArray(artifact.committed_collection_ids)
    ? artifact.committed_collection_ids
    : [];

  const collectionMemberships = committedCollectionIds.map(id => {
    const collection = collections.find(c => c.id === id);
    return {
      id,
      name: collection?.name || id,
      status: 'committed' as const,
    };
  });

  const handleDelete = async () => {
    const isPermanent = artifact.state === 'draft';
    if (isPermanent) {
      const confirmed = await dangerConfirm.confirm({
        title: CARD_CONFIRM.DELETE_PERMANENT_TITLE,
        description: CARD_CONFIRM.DELETE_PERMANENT_DESCRIPTION,
        confirmLabel: CARD_CONFIRM.DELETE_PERMANENT_CONFIRM,
        cancelLabel: BUTTON_LABELS.CANCEL
      });
      if (!confirmed) return;
    }
    await removeArtifact(String(artifact.id));
    onClose?.();
  };

  const handleRevert = async () => {
    await revertArtifact(String(artifact.id));
  };

  const handleArchive = async () => {
    await updateArtifact({ id: String(artifact.id), state: 'archived' });
  };

  const handleRestore = async () => {
    await updateArtifact({ id: String(artifact.id), state: 'committed' });
  };

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
    setShowCollectionPicker(false);
  };

  const handleAddTag = () => {
    if (newTag.trim() && !editTags.includes(newTag.trim())) {
      setEditTags([...editTags, newTag.trim()]);
      setNewTag('');
      setHasUnsavedChanges(true);
    }
  };

  const handleRemoveTag = (tag: string) => {
    setEditTags(editTags.filter(t => t !== tag));
    setHasUnsavedChanges(true);
  };

  return (
    <>
      <div className="flex flex-col h-full bg-white">
        {/* Header */}
        <div className="relative flex items-center justify-between px-4 py-3 border-b border-gray-200 flex-shrink-0">
          <div className="flex items-center gap-2 flex-1 min-w-0">
            {isEditing ? (
              <input
                type="text"
                value={editTitle}
                onChange={(e) => {
                  setEditTitle(e.target.value);
                  setHasUnsavedChanges(true);
                }}
                className="text-base font-semibold text-gray-900 bg-transparent border-b border-gray-300 focus:border-blue-500 outline-none flex-1"
                placeholder="Artifact title..."
              />
            ) : (
              <h2 className="text-base font-semibold text-gray-900 truncate">
                {title}
              </h2>
            )}
            {artifact.state === 'draft' && (
              <span className="px-2 py-0.5 text-xs font-medium rounded flex-shrink-0 bg-gradient-to-br from-green-400/25 via-emerald-400/25 to-teal-400/25 border border-green-200/50 text-green-700">
                New
              </span>
            )}
            {artifact.state === 'committed' && (
              <span className="px-2 py-0.5 text-xs font-medium rounded flex-shrink-0 bg-gradient-to-br from-amber-400/25 via-orange-400/25 to-yellow-400/25 border border-amber-200/50 text-amber-700">
                Modified
              </span>
            )}
            {artifact.state === 'archived' && (
              <span className="px-2 py-0.5 text-xs font-medium rounded flex-shrink-0 bg-gradient-to-br from-rose-400/25 via-red-400/25 to-pink-400/25 border border-rose-200/50 text-rose-700">
                Archived
              </span>
            )}
            {hasUnsavedChanges && (
              <span className="px-2 py-0.5 text-xs font-medium rounded flex-shrink-0 bg-gradient-to-br from-blue-400/25 via-cyan-400/25 to-teal-400/25 border border-blue-200/50 text-blue-700">
                Unsaved
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            {isEditing ? (
              <>
                <button
                  onClick={handleSave}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded hover:bg-blue-700 transition-colors"
                >
                  <Save className="w-4 h-4" />
                  Save
                </button>
                <button
                  onClick={handleCancel}
                  className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-gray-100 rounded hover:bg-gray-200 transition-colors"
                >
                  Cancel
                </button>
              </>
            ) : !isCollectionArtifact ? (
              <button
                onClick={() => setIsEditing(true)}
                className="px-3 py-1.5 text-sm font-medium text-blue-600 bg-blue-50 rounded hover:bg-blue-100 transition-colors"
              >
                Edit
              </button>
            ) : null}
            <button
              onClick={onClose}
              className="p-1 hover:bg-gray-100 rounded transition-colors"
              aria-label="Close"
            >
              <span className="text-xl leading-none text-gray-500">×</span>
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {isEditingContent && isMarkdownContent ? (
            <div className="relative" data-color-mode="light">
              <EditorFactory
                content={editContent}
                mime={ctx.content_type}
                filename={ctx.filename}
                onChange={(newContent) => {
                  setEditContent(newContent);
                }}
                onEscape={() => {
                  setIsEditingContent(false);
                  setEditContent(artifact?.content || '');
                }}
                autoFocus={true}
              />
              {/* Auto-save indicator */}
              {isContentSaving && (
                <div className="absolute top-2 right-2 px-2 py-1 text-xs bg-blue-50 text-blue-700 rounded shadow-sm">
                  Saving...
                </div>
              )}
              {!isContentSaving && contentLastSaved && (
                <div className="absolute top-2 right-2 px-2 py-1 text-xs bg-green-50 text-green-700 rounded shadow-sm">
                  Saved
                </div>
              )}
            </div>
          ) : isEditing && isMarkdownContent ? (
            <div data-color-mode="light">
              <MDEditor
                value={editContent}
                onChange={(val) => {
                  setEditContent(val || '');
                  setHasUnsavedChanges(true);
                }}
                preview="live"
                height={400}
                visibleDragbar={false}
              />
            </div>
          ) : isEditing ? (
            <textarea
              value={editContent}
              onChange={(e) => {
                setEditContent(e.target.value);
                setHasUnsavedChanges(true);
              }}
              className="w-full h-96 p-3 border border-gray-300 rounded font-mono text-sm resize-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
              placeholder="Artifact content..."
            />
          ) : (
            <div
              className={isCollectionArtifact ? "p-2 -m-2" : "group relative cursor-pointer p-2 -m-2 rounded hover:bg-gray-50 transition-colors"}
              onClick={() => {
                if (!isCollectionArtifact && isMarkdownContent) {
                  setIsEditingContent(true);
                  setEditContent(resolvedContent);
                }
              }}
            >
              {transformArtifact ? (
                <TransformCardSummary artifact={artifact} />
              ) : (
                <ContentRenderer
                  content={resolvedContent}
                  mime={ctx.content_type}
                  filename={ctx.filename}
                  uri={ctx.uri}
                />
              )}
              {!transformArtifact && !isCollectionArtifact && isMarkdownContent && (
                <div className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity">
                  <Pencil className="w-4 h-4 text-gray-400" />
                </div>
              )}
            </div>
          )}
        </div>

        {/* Metadata Section */}
        <div className="px-4 py-3 border-t border-gray-200 space-y-3 flex-shrink-0 overflow-y-auto max-h-64">
          {/* Description */}
          {isEditing ? (
            <div>
              <label className="block text-xs font-medium text-gray-700 mb-1">Description</label>
              <textarea
                value={editDescription}
                onChange={(e) => {
                  setEditDescription(e.target.value);
                  setHasUnsavedChanges(true);
                }}
                className="w-full p-2 text-sm border border-gray-300 rounded resize-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
                rows={2}
                placeholder="Brief description..."
              />
            </div>
          ) : ctx.description ? (
            <div className="text-sm text-gray-600">{ctx.description}</div>
          ) : null}

          {/* Tags */}
          <div className="flex items-start gap-2">
            <Tag className="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
            <div className="flex-1">
              {isEditing ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap gap-1">
                    {editTags.map((tag) => (
                      <span
                        key={tag}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
                      >
                        #{tag}
                        <button
                          onClick={() => handleRemoveTag(tag)}
                          className="hover:text-blue-900"
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={newTag}
                      onChange={(e) => setNewTag(e.target.value)}
                      onKeyPress={(e) => e.key === 'Enter' && handleAddTag()}
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
                </div>
              ) : editTags.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {editTags.map((tag) => (
                    <span
                      key={tag}
                      className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
                    >
                      #{tag}
                    </span>
                  ))}
                </div>
              ) : (
                <span className="text-xs text-gray-400">No tags</span>
              )}
            </div>
          </div>

          {/* Collections */}
          <div className="flex items-start gap-2">
            <Folder className="w-4 h-4 text-gray-400 mt-0.5 flex-shrink-0" />
            <div className="flex-1 flex items-center justify-between gap-2">
              <div className="flex flex-wrap gap-1.5">
                {collectionMemberships.length > 0 ? (
                  collectionMemberships.map(({ id, name, status }) => (
                    <CollectionChip key={id} id={id} name={name} status={status} />
                  ))
                ) : (
                  <span className="text-xs text-gray-400">No collections</span>
                )}
              </div>
              {!isCollectionArtifact && (
                <button
                  onClick={() => setShowCollectionPicker(true)}
                  className="text-xs text-blue-600 hover:text-blue-700 font-medium whitespace-nowrap flex-shrink-0"
                >
                  Change
                </button>
              )}
            </div>
          </div>

          {/* Timestamp */}
          <div className="flex items-center gap-2 text-xs text-gray-500">
            <Calendar className="w-4 h-4" />
            <span>{artifact.modified_time ? 'Modified' : 'Created'} {timestamp}</span>
          </div>

          {/* File metadata */}
          {(ctx.content_type || ctx.size) && (
            <div className="flex items-center gap-3 text-xs text-gray-500">
              {ctx.content_type && <span>Type: {ctx.content_type}</span>}
              {ctx.size && <span>Size: {(ctx.size / 1024).toFixed(1)} KB</span>}
            </div>
          )}
        </div>

        {/* Quick Actions */}
        <div className="px-4 py-3 border-t border-gray-200 flex-shrink-0">
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => setShowCollectionPicker(true)}
              className="flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded transition-all bg-gradient-to-br from-purple-400/20 via-pink-400/20 to-blue-400/20 hover:from-purple-500/30 hover:via-pink-500/30 hover:to-blue-500/30 border border-purple-200/40 text-purple-700"
            >
              <FolderInput className="w-4 h-4" />
              Move
            </button>

            {!isCollectionArtifact && (
              <button
                onClick={handleExtractUnits}
                className="flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded transition-all bg-gradient-to-br from-purple-400/20 via-pink-400/20 to-blue-400/20 hover:from-purple-500/30 hover:via-pink-500/30 hover:to-blue-500/30 border border-purple-200/40 text-purple-700"
              >
                <Wand2 className="w-4 h-4" />
                Extract
              </button>
            )}

            {artifact.state === 'committed' && (
              <button
                onClick={handleRevert}
                className="flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded transition-all bg-gradient-to-br from-amber-400/20 via-orange-400/20 to-yellow-400/20 hover:from-amber-500/30 hover:via-orange-500/30 hover:to-yellow-500/30 border border-amber-200/40 text-amber-700"
              >
                <RotateCcw className="w-4 h-4" />
                Revert
              </button>
            )}

            {artifact.state === 'committed' && (
              <button
                onClick={handleArchive}
                className="flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded transition-all bg-gradient-to-br from-amber-400/20 via-orange-400/20 to-yellow-400/20 hover:from-amber-500/30 hover:via-orange-500/30 hover:to-yellow-500/30 border border-amber-200/40 text-amber-700"
              >
                <Archive className="w-4 h-4" />
                Archive
              </button>
            )}

            {artifact.state === 'archived' && (
              <button
                onClick={handleRestore}
                className="flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded transition-all bg-gradient-to-br from-green-400/20 via-emerald-400/20 to-teal-400/20 hover:from-green-500/30 hover:via-emerald-500/30 hover:to-teal-500/30 border border-green-200/40 text-green-700"
              >
                <RefreshCw className="w-4 h-4" />
                Restore
              </button>
            )}

            <button
              onClick={handleDelete}
              className="flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded transition-all bg-gradient-to-br from-rose-400/20 via-red-400/20 to-pink-400/20 hover:from-rose-500/30 hover:via-red-500/30 hover:to-pink-500/30 border border-rose-200/40 text-rose-700"
            >
              <Trash2 className="w-4 h-4" />
              {artifact.state === 'draft' ? 'Delete' : 'Remove'}
            </button>
          </div>
        </div>
      </div>

      {/* Collection Picker Modal */}
      <CollectionPicker
        open={showCollectionPicker}
        onClose={() => setShowCollectionPicker(false)}
        onSelect={handleSelectCollections}
        selectedCollectionIds={committedCollectionIds}
        multiple={true}
        title="Move to Collection"
      />
    </>
  );
}
