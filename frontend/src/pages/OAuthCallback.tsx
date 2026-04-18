import { useEffect, useState, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { post } from '@/api/api';

/**
 * OAuthCallback — handles the redirect from an upstream OAuth provider
 * (e.g. Google) after the user authorizes an Authorizer artifact.
 *
 * Flow:
 *   1. Parse `code` + `state` (nonce) from URL params.
 *   2. Retrieve PKCE `code_verifier` + artifact context from sessionStorage.
 *   3. POST /auth/authorizer/complete-oauth to finish the exchange.
 *   4. Show success → navigate back to workspace.
 */
export default function OAuthCallback() {
  const navigate = useNavigate();
  const location = useLocation();
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading');
  const [errorMessage, setErrorMessage] = useState('');
  const processedRef = useRef(false);

  useEffect(() => {
    if (processedRef.current) return;
    processedRef.current = true;

    const params = new URLSearchParams(location.search);
    const code = params.get('code');
    const nonce = params.get('state');

    if (!code || !nonce) {
      setStatus('error');
      setErrorMessage('Missing authorization code or state parameter.');
      return;
    }

    // Retrieve stored PKCE data
    const storageKey = `authorizer_oauth_${nonce}`;
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) {
      setStatus('error');
      setErrorMessage('OAuth session expired or invalid. Please try connecting again.');
      return;
    }

    let stored: {
      code_verifier: string;
      authorizer_artifact_id: string;
      workspace_id: string;
    };
    try {
      stored = JSON.parse(raw);
    } catch {
      setStatus('error');
      setErrorMessage('Failed to parse OAuth session data.');
      return;
    }

    // Clean up session storage
    sessionStorage.removeItem(storageKey);

    const redirectUri = `${window.location.origin}/oauth/callback`;

    // Complete the upstream OAuth exchange via the dedicated auth endpoint.
    post('/auth/authorizer/complete-oauth', {
      workspace_id: stored.workspace_id,
      authorizer_artifact_id: stored.authorizer_artifact_id,
      authorization_code: code,
      code_verifier: stored.code_verifier,
      redirect_uri: redirectUri,
    })
      .then(() => {
        setStatus('success');
        setTimeout(() => navigate('/', { replace: true }), 1500);
      })
      .catch((err) => {
        console.error('Authorizer OAuth completion failed:', err);
        setStatus('error');
        setErrorMessage('Failed to complete account connection. Please try again.');
      });
  }, [location.search, navigate]);

  if (status === 'error') {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center px-4 gap-4">
        <div
          className="bg-red-100 border border-red-400 text-red-600 px-4 py-3 rounded"
          role="alert"
        >
          <strong className="font-bold">Error: </strong>
          <span>{errorMessage}</span>
        </div>
        <button
          onClick={() => navigate('/', { replace: true })}
          className="text-sm underline text-muted-foreground hover:text-foreground"
        >
          Return to workspace
        </button>
      </div>
    );
  }

  if (status === 'success') {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="text-green-600 text-2xl mb-2">Account connected</div>
          <p className="text-sm text-muted-foreground">Redirecting...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-gray-900 mx-auto mb-4" />
        <p>Connecting account...</p>
      </div>
    </div>
  );
}
