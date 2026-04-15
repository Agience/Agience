import './App.css';
import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Toaster } from './components/ui/sonner';

import { AuthProvider } from './context/auth/AuthProvider';
import { PreferencesProvider } from './context/preferences/PreferencesProvider';
import { WorkspacesProvider } from './context/workspaces/WorkspacesProvider';

import LoginProtected from './auth/LoginProtected';

import Login from './pages/Login';
import Privacy from './pages/Privacy';
import WorkspacePage from './pages/WorkspacePage';
import Terms from './pages/Terms';
import { WorkspaceProvider } from './context/workspace/WorkspaceProvider';
import { CollectionsProvider } from './context/collections/CollectionsProvider';
import { ShortcutsProvider } from './context/shortcuts/ShortcutsProvider';
import KeyboardShortcutsDialog from './components/modals/KeyboardShortcutsDialog';
import { DialogProvider } from './context/dialog/DialogProvider';

import { getSetupStatus } from './api/setup';

const AuthCallback = lazy(() => import('./routes/AuthCallback'));
const OAuthCallback = lazy(() => import('./pages/OAuthCallback'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const SetupWizard = lazy(() => import('./pages/SetupWizard'));

const Spinner = () => (
  <div className="min-h-screen flex items-center justify-center">
    <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-gray-900" />
  </div>
);

// TODO: Migrate to invite claim flow. The X-Grant-Key header has been removed
// from the backend; grant keys captured here are no longer sent as a header.
// When the /grants/claim endpoint is ready, redirect ?grant_key=xxx to
// /grants/claim?token=xxx instead of storing in sessionStorage.
function GrantKeyCapture() {
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const grantKey = params.get('grant_key');
    if (!grantKey) return;

    try {
      const raw = sessionStorage.getItem('grant_keys') ?? localStorage.getItem('grant_keys');
      const existing = raw ? (JSON.parse(raw) as unknown) : [];
      const asArray = Array.isArray(existing) ? existing.map(String).filter(Boolean) : [];
      const next = [grantKey, ...asArray.filter((k) => k !== grantKey)].slice(0, 10);
      sessionStorage.setItem('grant_keys', JSON.stringify(next));
    } catch {
      sessionStorage.setItem('grant_keys', JSON.stringify([grantKey]));
    }

    // Remove the secret from the URL.
    params.delete('grant_key');
    const search = params.toString();
    navigate(
      {
        pathname: location.pathname,
        search: search ? `?${search}` : '',
        hash: location.hash,
      },
      { replace: true }
    );
  }, [location.pathname, location.search, location.hash, navigate]);

  return null;
}

/**
 * Setup detection — checks if the platform needs first-boot setup.
 * If needs_setup is true, all routes redirect to the setup wizard.
 */
function SetupGate({ children }: { children: React.ReactNode }) {
  const [checking, setChecking] = useState(true);
  const [needsSetup, setNeedsSetup] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  const checkStatus = useCallback(() => {
    setChecking(true);
    getSetupStatus()
      .then((status) => {
        setNeedsSetup(status.needs_setup);
        setChecking(false);
        if (status.needs_setup && !location.pathname.startsWith('/setup')) {
          navigate('/setup', { replace: true });
        }
      })
      .catch(() => {
        setChecking(false);
      });
  }, [location.pathname, navigate]);

  useEffect(() => {
    checkStatus();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Listen for setup-complete event from SetupWizard.
  // The event itself is authoritative — no re-fetch needed.
  // SetupWizard already cleared the interval, stored the token, and will navigate.
  // Just flip the flag so SetupGate renders children without the spinner flash.
  useEffect(() => {
    const handler = () => {
      setNeedsSetup(false);
      setChecking(false);
    };
    window.addEventListener('setup-complete', handler);
    return () => window.removeEventListener('setup-complete', handler);
  }, []);

  if (checking) return <Spinner />;

  // If setup needed and not on setup page, redirect
  if (needsSetup && !location.pathname.startsWith('/setup')) {
    return <Navigate to="/setup" replace />;
  }

  return <>{children}</>;
}

function AppRoutes() {
  return (
    <SetupGate>
      <Routes>
        {/* Setup wizard (first-boot) */}
        <Route
          path="/setup"
          element={
            <Suspense fallback={<Spinner />}>
              <SetupWizard />
            </Suspense>
          }
        />

        {/* Public Routes */}
        <Route path="/login" element={<Login />} />
        <Route path="/terms" element={<Terms />} />
        <Route path="/privacy" element={<Privacy />} />
        <Route path="/auth/error" element={<Login />} />
        <Route
          path="/auth/callback"
          element={
            <Suspense fallback={<Spinner />}>
              <AuthCallback />
            </Suspense>
          }
        />
        <Route
          path="/oauth/callback"
          element={
            <Suspense fallback={<Spinner />}>
              <OAuthCallback />
            </Suspense>
          }
        />

        <Route path="/" element={
          <LoginProtected>
            <CollectionsProvider>
              <WorkspacesProvider>
                <WorkspaceProvider>
                    <WorkspacePage />
                  </WorkspaceProvider>
              </WorkspacesProvider>
            </CollectionsProvider>
          </LoginProtected>
        } />
        <Route path="/settings" element={
          <LoginProtected>
            <Suspense fallback={<Spinner />}>
              <SettingsPage />
            </Suspense>
          </LoginProtected>
        } />
        {/* Fallback */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </SetupGate>
  );
}

export default function App() {
  useEffect(() => {
    const guard = (e: DragEvent) => {
      const hasFiles = e.dataTransfer?.types?.includes('Files');
      if (!hasFiles) return;
      const path = (e.composedPath?.() ?? []) as Element[];
      const insideWorkspace = path.some(
        (el) =>
          el instanceof HTMLElement &&
          (el.dataset?.acceptDrop === 'workspace' ||
           el.closest?.('[data-accept-drop="workspace"]'))
      );
      if (!insideWorkspace) e.preventDefault();
    };
    window.addEventListener('dragover', guard, { capture: true });
    window.addEventListener('drop', guard, { capture: true });
    return () => {
      window.removeEventListener('dragover', guard, { capture: true });
      window.removeEventListener('drop', guard, { capture: true });
    };
  }, []);

  return (
    <AuthProvider>
      <PreferencesProvider>
        <ShortcutsProvider>
          <DialogProvider>
            <BrowserRouter>
              <GrantKeyCapture />
              <Toaster />
              <KeyboardShortcutsDialog />
              <Suspense fallback={<Spinner />}>
                <AppRoutes />
              </Suspense>
            </BrowserRouter>
          </DialogProvider>
        </ShortcutsProvider>
      </PreferencesProvider>
    </AuthProvider>
  );
}
