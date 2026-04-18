// context/preferences/preferences.types.ts
import { ReactNode } from 'react';

export interface LayoutPreferences {
  sidebarWidth?: number;
  sidebarCollapsed?: boolean;
  browserWidth?: number;
  previewWidth?: number;
  previewCollapsed?: boolean;
}

export interface SidebarSectionsPreferences {
  expanded?: string[];
  hiddenWorkspaces?: string[];
  hiddenCollections?: string[];
  hiddenMcpServers?: string[];
  showAllWorkspaces?: boolean;
  showAllCollections?: boolean;
  showAllMcpServers?: boolean;
  resourcesViewMode?: 'by-collection' | 'by-type' | 'by-time' | 'by-author';
}

export interface BrowserPreferences {
  // Explicit list of workspace IDs currently docked in the UI.
  dockedWorkspaceCardIds?: string[];
}

export interface Preferences {
  layout?: LayoutPreferences;
  sidebarSections?: SidebarSectionsPreferences;
  browser?: BrowserPreferences;
  [key: string]: unknown;
}

export interface PreferencesContextType {
  preferences: Preferences;
  updatePreferences: (prefs: Partial<Preferences>) => Promise<void>;
  isLoading: boolean;
}

export interface PreferencesProviderProps {
  children: ReactNode;
}
