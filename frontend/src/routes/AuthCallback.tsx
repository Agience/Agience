import React, { useEffect, useState, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';

import { postForm, post } from '../api/api';
import { getRuntimeConfig } from '../config/runtime';
import { useAuth } from '../hooks/useAuth';
import { postLoginRedirectTarget } from '../auth/postLoginRedirect';

const { clientId: CLIENT_ID } = getRuntimeConfig();

const AuthCallback: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { refreshUser } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const processedRef = useRef(false);

  useEffect(() => {
    // Run only once
    if (processedRef.current) return;
    processedRef.current = true;

    const params = new URLSearchParams(location.search);
    const code = params.get('code');
    const state = params.get('state');
    const storedState = localStorage.getItem('pkce_state');
    const codeVerifier = localStorage.getItem('pkce_verifier');

    if (!code || !state || state !== storedState || !codeVerifier) {
      setError('OAuth2 callback validation failed.');
      setTimeout(() => navigate('/login', { replace: true }), 3000);
      return;
    }

    const linkingProvider = sessionStorage.getItem('linking_provider');

    if (linkingProvider) {
      // --- Account-linking flow ---
      sessionStorage.removeItem('linking_provider');
      localStorage.removeItem('pkce_state');
      localStorage.removeItem('pkce_verifier');

      post<Record<string, unknown>>('/auth/me/link-provider', {
        code,
        code_verifier: codeVerifier,
        redirect_uri: `${window.location.origin}/auth/callback`,
      })
        .then(() => {
          refreshUser();
          navigate('/?linked=1', { replace: true });
        })
        .catch((err) => {
          console.error('Account linking failed:', err);
          const detail = err?.response?.data?.detail || 'Failed to link account.';
          navigate(`/?link_error=${encodeURIComponent(detail)}`, { replace: true });
        });
      return;
    }

    // --- Normal login flow ---
    const body = new URLSearchParams({
      grant_type:    'authorization_code',
      code,
      redirect_uri:  `${window.location.origin}/auth/callback`,
      client_id:     CLIENT_ID,
      code_verifier: codeVerifier,
    });

    postForm<{ access_token: string }>(`/auth/token`, body)
      .then(({ access_token }) => {
        // Cleanup PKCE data
        localStorage.removeItem('pkce_state');
        localStorage.removeItem('pkce_verifier');

        // Persist token and do full page load to cleanly initialize auth state.
        // If there's a pending invite token, send the user to the claim page
        // so it can finish accepting; otherwise go home.
        localStorage.setItem('access_token', access_token);
        const target = postLoginRedirectTarget();
        console.log('[AuthCallback] token stored, redirecting to', target);
        window.location.href = target;
      })
      .catch((err) => {
        console.error('Token exchange failed:', err);
        setError('Failed to complete authentication.');
        setTimeout(() => navigate('/login', { replace: true }), 3000);
      });
  }, [location.search, navigate, refreshUser]);

  if (error) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center px-4">
        <div className="bg-red-100 border border-red-400 text-red-600 px-4 py-3 rounded relative mb-4" role="alert">
          <strong className="font-bold">Error:</strong>
          <span className="block sm:inline ml-2">{error}</span>
        </div>
        <p>Redirecting to login page...</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-gray-900 mx-auto mb-4" />
        <p>Completing authentication...</p>
      </div>
    </div>
  );
};

export default AuthCallback;
