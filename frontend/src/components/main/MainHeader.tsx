import { useState, useRef, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { FiMoon, FiSun, FiUser, FiChevronDown, FiSettings } from 'react-icons/fi';
import { HelpCircle, Link2, MessageCircle, Send, Share2 } from 'lucide-react';
import { useAuth } from '../../hooks/useAuth';
import { useWorkspace } from '../../hooks/useWorkspace';
import { useWorkspaces } from '@/hooks/useWorkspaces';
import type { ActiveSource } from '../../types/workspace';
import type { Artifact } from '@/context/workspace/workspace.types';
import { IconButton } from '@/components/ui/icon-button';
import SettingsModal from '../modals/SettingsModal';
import { formatShortcutCombo } from '@/context/shortcuts/shortcut-utils';
import HelpDialog from '../modals/HelpDialog';
import { ShareDialog } from '../common/ShareDialog';
import { CHAT_CONTENT_TYPE } from '@/utils/content-type';
import { useUpgradePrompt } from '@/hooks/useUpgradePrompt';
import { getArtifact } from '@/api/artifacts';

type SettingsSection = 'profile' | 'general' | 'llm-keys' | 'billing' | 'demo-data';

type ArtifactOpenOptions = {
  startInEditMode?: boolean;
};

type HeaderBarProps = {
  activeSource: ActiveSource;
  /** Actual sidebar width in pixels so tabs align with the divider */
  sidebarWidthPx?: number;
  /** Whether the panel resize handle is being dragged (to sync divider color). */
  isPanelResizing?: boolean;
  settingsInitialSection?: SettingsSection;
  settingsScopedCollectionId?: string;
  onSettingsSectionClear?: () => void;
  /** Called after a chat artifact is created from the "Ask anything" input. */
  onArtifactCreated?: (artifact: Artifact, options?: ArtifactOpenOptions) => void;
};

export default function HeaderBar({
  sidebarWidthPx,
  isPanelResizing = false,
  settingsInitialSection,
  settingsScopedCollectionId,
  onSettingsSectionClear,
  onArtifactCreated,
}: HeaderBarProps) {
  const [darkMode, setDarkMode] = useState(false);

  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [showShare, setShowShare] = useState(false);

  // Internal section override — used when agience:open-settings event fires
  const [eventSection, setEventSection] = useState<SettingsSection | undefined>(undefined);

  // Mount the 402 upgrade-prompt toast listener
  useUpgradePrompt();
  const userMenuRef = useRef<HTMLDivElement>(null);
  const { user } = useAuth() ?? {};
  const navigate = useNavigate();
  const shortcutHint = formatShortcutCombo('mod+/').join(' + ');
  const { createArtifact } = useWorkspace();
  const { activeWorkspaceId } = useWorkspaces();

  // Open settings modal when initialSection is set from parent
  useEffect(() => {
    if (settingsInitialSection) {
      setShowSettings(true);
    }
  }, [settingsInitialSection]);

  // Listen for agience:open-settings events (e.g. from the upgrade toast)
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<{ section?: string }>).detail;
      const section = detail?.section as SettingsSection | undefined;
      if (section) setEventSection(section);
      setShowSettings(true);
    };
    window.addEventListener('agience:open-settings', handler);
    return () => window.removeEventListener('agience:open-settings', handler);
  }, []);

  const toggleDarkMode = () => {
    setDarkMode(!darkMode);
    // TODO: Implement dark mode toggle logic
  };

  // AdvancedSearch handles querying and dropdown UI internally

  const logout = useCallback(() => {
    localStorage.removeItem('access_token');
    navigate('/');
    window.location.reload();
  }, [navigate]);

  // Close user menu on outside click or Escape
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setShowUserMenu(false);
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setShowUserMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKey);
    
    // Cleanup on unmount
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKey);
    };
  }, []);

  // When expanded, match the live sidebar width so the tabs slide with the divider.
  const expandedSidebarWidth = typeof sidebarWidthPx === 'number' && sidebarWidthPx > 0
    ? sidebarWidthPx + 1
    : 256;

  // Quick add — creates a Chat artifact that auto-opens as floating ChatWindow
  const [quickAddText, setQuickAddText] = useState('');
  const canSubmitQuickAdd = Boolean(activeWorkspaceId) && Boolean(quickAddText.trim());

  const handleQuickAddSubmit = useCallback(async () => {
    const text = quickAddText.trim();
    if (!text || !activeWorkspaceId) return;

    try {
      const chatContext = {
        type: 'chat',
        title: text.length > 60 ? text.slice(0, 57) + '...' : text,
        content_type: CHAT_CONTENT_TYPE,
        chat: {
          workspace_id: activeWorkspaceId ?? undefined,
          messages: [{ role: 'user', content: text }],
        },
      };
      const artifact = await createArtifact({
        content: text,
        content_type: CHAT_CONTENT_TYPE,
        context: JSON.stringify(chatContext),
      });
      setQuickAddText('');
      if (artifact && onArtifactCreated) {
        onArtifactCreated(artifact);
      }
    } catch (error) {
      console.error('[HeaderBar] Chat artifact creation failed', error);
    }
  }, [quickAddText, createArtifact, activeWorkspaceId, onArtifactCreated]);

  const handleOpenBindings = useCallback(async () => {
    if (!activeWorkspaceId || !onArtifactCreated) return;
    try {
      const wsArtifact = await getArtifact(activeWorkspaceId);
      onArtifactCreated(wsArtifact);
    } catch (err) {
      console.error('Failed to open bindings:', err);
    }
  }, [activeWorkspaceId, onArtifactCreated]);

  return (
    <header className="flex items-center h-16 bg-white border-b border-gray-200 z-20">
      {/* Left Section: Dynamic width matching sidebar */}
      <div
        className={`flex items-center flex-shrink-0 h-full border-r transition-colors ${isPanelResizing ? 'border-purple-500' : 'border-gray-200 hover:border-purple-300'}`}
        style={{ width: `${expandedSidebarWidth}px`, justifyContent: 'flex-start', paddingLeft: '16px', paddingRight: '11px' }}
      >
        <img src="/logo_h.png" alt="Agience" className="h-10" />
      </div>

      {/* Center Section: Ask anything input, aligned with workspace divider and sliding with sidebar */}
      <div className="flex-1 pr-4 flex items-center">
        <div className="relative w-full max-w-xl ml-[17px]">
          {/* Ask anything input, styled to match AdvancedSearch */}
          <MessageCircle className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-gray-400" />
          <input
            value={quickAddText}
            onChange={(e) => setQuickAddText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void handleQuickAddSubmit();
              }
            }}
            placeholder="Ask anything..."
            className="w-full pl-11 pr-20 py-2.5 text-sm border border-border rounded-lg bg-white/95 shadow-sm focus:outline-none focus:ring-2 focus:ring-purple-500 focus:border-transparent transition-all placeholder:text-muted-foreground/70 disabled:cursor-not-allowed disabled:opacity-70"
          />

          {/* Right-side send button */}
          <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
            <button
              onClick={() => void handleQuickAddSubmit()}
              disabled={!canSubmitQuickAdd}
              className="p-1 hover:bg-gray-100 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title={activeWorkspaceId ? 'Send (Enter)' : 'Dock a workspace to save a chat'}
            >
              <Send className="h-4 w-4 text-gray-500" />
            </button>
          </div>
        </div>
      </div>

      {/* Right Section: Actions */}
      <div className="flex items-center gap-2 pr-4 flex-shrink-0">
        {/* Bindings — active workspace only */}
        {activeWorkspaceId && (
          <IconButton
            size="lg"
            variant="ghost"
            onClick={handleOpenBindings}
            title="Workspace bindings"
            aria-label="Open workspace bindings"
          >
            <Link2 />
          </IconButton>
        )}

        {/* Share — active workspace only */}
        {activeWorkspaceId && (
          <IconButton
            size="lg"
            variant="ghost"
            onClick={() => setShowShare(true)}
            title="Share this workspace"
            aria-label="Share workspace"
          >
            <Share2 />
          </IconButton>
        )}

        {/* Help */}
        <IconButton
          size="lg"
          variant="ghost"
          onClick={() => setShowHelp(true)}
          title={`Help (Shortcuts: ${shortcutHint} or ?)`}
          aria-label="Open help"
        >
          <HelpCircle />
        </IconButton>

        {/* Dark Mode Toggle */}
        <IconButton
          size="lg"
          variant="ghost"
          onClick={toggleDarkMode}
          title={darkMode ? 'Light Mode' : 'Dark Mode'}
        >
          {darkMode ? <FiSun /> : <FiMoon />}
        </IconButton>

        {/* User Menu */}
        <div ref={userMenuRef} className="relative">
          <button
            onClick={() => setShowUserMenu(!showUserMenu)}
            className="flex items-center gap-2 rounded-lg transition-colors group hover:bg-gray-100"
            title="User Menu"
            aria-expanded={showUserMenu}
          >
            <div className="h-10 w-10 rounded-sm border border-gray-300 bg-white flex items-center justify-center flex-shrink-0 transition-colors">
              <FiUser className="w-4 h-4 text-gray-500 group-hover:text-gray-900" />
            </div>
            <span className="text-sm font-medium text-gray-500 group-hover:text-gray-900 hidden md:inline transition-colors">
              {user?.name || 'User'}
            </span>
            <FiChevronDown className={`w-4 h-4 text-gray-600 transition-transform hidden md:inline ${showUserMenu ? 'rotate-180' : ''}`} />
          </button>

          {showUserMenu && (
            <div className="absolute right-0 mt-2 w-56 bg-white border border-gray-200 rounded-lg shadow-lg z-50">
              <div className="px-4 py-3 text-sm text-gray-600 border-b border-gray-200">
                {user?.email}
              </div>
              <button
                onClick={() => {
                  setShowUserMenu(false);
                  setShowSettings(true);
                }}
                className="w-full text-left px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 transition-colors flex items-center gap-2"
              >
                <FiSettings className="w-4 h-4" />
                Settings
              </button>
              <button
                onClick={logout}
                className="w-full text-left px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 rounded-b-lg transition-colors"
              >
                Logout
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Settings Modal */}
      <SettingsModal
        isOpen={showSettings}
        onClose={() => {
          setShowSettings(false);
          setEventSection(undefined);
          onSettingsSectionClear?.();
        }}
        initialSection={eventSection ?? settingsInitialSection}
        scopedToCollectionId={settingsScopedCollectionId}
        onArtifactCreated={onArtifactCreated}
      />

      <HelpDialog
        isOpen={showHelp}
        onClose={() => setShowHelp(false)}
        defaultTab="glossary"
      />

      {activeWorkspaceId && (
        <ShareDialog
          open={showShare}
          onOpenChange={setShowShare}
          workspaceId={activeWorkspaceId}
        />
      )}
    </header>
  );
}