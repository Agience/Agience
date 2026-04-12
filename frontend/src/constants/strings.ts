/**
 * Centralized UI strings for consistency across the application.
 * Organized by feature area for easier maintenance.
 */

import { PRODUCT_NOUN, noun } from '@/product/copy';

// Artifact deletion/removal confirmations
export const CARD_CONFIRM = {
  DELETE_PERMANENT_TITLE: `Delete ${PRODUCT_NOUN.singular.toLowerCase()} permanently?`,
  DELETE_PERMANENT_DESCRIPTION: `This ${PRODUCT_NOUN.singular.toLowerCase()} has never been published. Deleting it will permanently remove all content.`,
  DELETE_PERMANENT_CONFIRM: 'Delete permanently',
  REVERT_TITLE: `Revert changes to this ${PRODUCT_NOUN.singular.toLowerCase()}?`,
  REVERT_DESCRIPTION: 'This will discard local edits and restore the last committed version.',
  REVERT_CONFIRM: 'Revert changes',
  ARCHIVE_TITLE: `Archive this ${PRODUCT_NOUN.singular.toLowerCase()}?`,
  ARCHIVE_DESCRIPTION: `This will hide the ${PRODUCT_NOUN.singular.toLowerCase()} from active views until restored.`,
  ARCHIVE_CONFIRM: 'Archive',
} as const;

// Bulk operations confirmations
export const BULK_CONFIRM = {
  DELETE_TITLE: (count: number) => `Delete ${count} ${noun(count).toLowerCase()}?`,
  DELETE_DESCRIPTION: (count: number) => `${count === 1 ? `This ${PRODUCT_NOUN.singular.toLowerCase()}` : `These ${PRODUCT_NOUN.plural.toLowerCase()}`} ${count === 1 ? 'has' : 'have'} never been published and will be permanently deleted.`,
  ARCHIVE_TITLE: (count: number) => `Archive ${count} ${noun(count).toLowerCase()}?`,
  ARCHIVE_DESCRIPTION: (count: number) => `${count === 1 ? `This ${PRODUCT_NOUN.singular.toLowerCase()}` : `These ${PRODUCT_NOUN.plural.toLowerCase()}`} ${count === 1 ? 'will' : 'will'} be moved to archived state and can be restored later.`,
} as const;

// API key/share deletion confirmations
export const API_KEY_CONFIRM = {
  DELETE_TITLE: (name: string) => `Delete API key "${name}"?`,
  DELETE_DESCRIPTION: 'This will immediately revoke access for any systems using this key. They will need to re-authenticate with a new key.',
  DELETE_CONFIRM: 'Delete key',
} as const;

// Workspace inbound key confirmations
export const INBOUND_KEY_CONFIRM = {
  DELETE_TITLE: 'Delete inbound key?',
  DELETE_DESCRIPTION: 'This will revoke access for any external systems using this key. This action cannot be undone.',
  DELETE_CONFIRM: 'Delete key',
} as const;

// Workspace streaming key confirmations
export const STREAM_KEY_CONFIRM = {
  ROTATE_TITLE: 'Generate/rotate streaming key?',
  ROTATE_DESCRIPTION: 'This will invalidate the previous streaming key for this workspace. Update any OBS profiles (and transcription gateways) that use it.',
  ROTATE_CONFIRM: 'Rotate key',
} as const;

// Collection status labels
export const COLLECTION_STATUS = {
  READ_ONLY: 'read-only',
  ARCHIVED: 'archived',
  BLOCKED: 'blocked',
} as const;

// Artifact state labels
export const CARD_STATE = {
  DRAFT: 'draft',
  COMMITTED: 'committed',
  ARCHIVED: 'archived',
} as const;

// Keyboard shortcut descriptions
export const SHORTCUTS = {
  SELECT_ALL: {
    LABEL: `Select all ${PRODUCT_NOUN.plural.toLowerCase()}`,
    DESCRIPTION: `Select all visible ${PRODUCT_NOUN.plural.toLowerCase()} in the current workspace`,
  },
  CLEAR_SELECTION: {
    LABEL: 'Clear selection',
    DESCRIPTION: `Clear all selected ${PRODUCT_NOUN.plural.toLowerCase()}`,
  },
  OPEN_SHORTCUTS: {
    LABEL: 'Show keyboard shortcuts',
    DESCRIPTION: 'View all available keyboard shortcuts',
  },
  OPEN_COMMAND_PALETTE: {
    LABEL: 'Open command palette',
    DESCRIPTION: 'Quick access to commands and navigation',
  },
  FOCUS_SEARCH: {
    LABEL: 'Focus global search',
    DESCRIPTION: 'Jump to the search input',
  },
} as const;

// Common button labels
export const BUTTON_LABELS = {
  CANCEL: 'Cancel',
  CLOSE: 'Close',
  DELETE: 'Delete',
  SAVE: 'Save',
  CONFIRM: 'Confirm',
} as const;

// Workspace/collection copy
export const WORKSPACE_COPY = {
  PENDING_CHANGES: 'pending changes',
  NO_CHANGES: 'No pending changes',
  COLLECTIONS_READ_ONLY: (count: number) => `${count} collection${count === 1 ? '' : 's'} read-only`,
} as const;
