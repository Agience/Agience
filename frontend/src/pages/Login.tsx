/**
 * pages/Login.tsx
 *
 * Unified login/signup page.
 *
 * Flow:
 *   1. User enters username or email → "Continue"
 *   2. Existing + has passkey → passkey button (prominent) + password fallback
 *   3. Existing + no passkey → password field + forgot password + OTP option
 *   4. New user → username + password (email optional) ("Create your account")
 */

import React, { useEffect, useState, useCallback } from 'react'
import { FcGoogle } from 'react-icons/fc'
import { useNavigate, useLocation, Link } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { get, post } from '../api/api'
import { getPasskeyLoginOptions, completePasskeyLogin, requestOTP, verifyOTP } from '../api/setup'
import { toast } from 'sonner'
import AuthLayout from '../components/layout/AuthLayout'
import CreateAccountForm from '../components/auth/CreateAccountForm'
import { postLoginRedirectTarget } from '../auth/postLoginRedirect'

type AuthProviderInfo = {
  name: string
  label: string
  type: string
}

type AuthProvidersResponse = {
  providers: AuthProviderInfo[]
  password: boolean
  otp: boolean
}

type AuthPhase = 'email' | 'login' | 'register' | 'otp'

const Login: React.FC = () => {
  const { login, setAuthData, isAuthenticated } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [error, setError] = useState<string | null>(null)

  // Setup operator token — present when redirected from the setup wizard (Google-only)
  const setupOperatorToken = new URLSearchParams(location.search).get('setup_token') ?? undefined

  // Strip consumed query params from the URL so they don't linger as a trailing "?"
  useEffect(() => {
    // Clean setup_token if present, and always strip a bare trailing "?"
    const params = new URLSearchParams(location.search)
    if (setupOperatorToken) params.delete('setup_token')
    const clean = params.toString()
    const target = `${location.pathname}${clean ? `?${clean}` : ''}`
    if (target !== `${location.pathname}${location.search}`) {
      window.history.replaceState(null, '', target)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const [providers, setProviders] = useState<AuthProviderInfo[]>([])
  const [providersReady, setProvidersReady] = useState(false)
  const [_passwordEnabled, setPasswordEnabled] = useState(true)
  const [otpEnabled, setOtpEnabled] = useState(false)

  const [phase, setPhase] = useState<AuthPhase>('email')
  const [identifier, setIdentifier] = useState('')  // username or email
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [optionalEmail, setOptionalEmail] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [hasPasskeys, setHasPasskeys] = useState(false)
  const [passkeyOptions, setPasskeyOptions] = useState<Record<string, unknown> | null>(null)

  // OTP state
  const [otpCode, setOtpCode] = useState(['', '', '', '', '', ''])
  const [_otpSent, setOtpSent] = useState(false)
  const [otpCountdown, setOtpCountdown] = useState(0)

  // Load providers on mount
  useEffect(() => {
    let mounted = true
    get<AuthProvidersResponse>('/auth/providers')
      .then((res) => {
        if (!mounted) return
        setProviders(res.providers || [])
        setPasswordEnabled(Boolean(res.password))
        setOtpEnabled(Boolean(res.otp))
        setProvidersReady(true)
      })
      .catch(() => {
        if (!mounted) return
        setProvidersReady(true) // proceed anyway
      })
    return () => { mounted = false }
  }, [])

  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const msg = params.get('error')
    if (msg) {
      setError(decodeURIComponent(msg))
      // Clean the error param from the URL
      params.delete('error')
      const clean = params.toString()
      window.history.replaceState(null, '', `${location.pathname}${clean ? `?${clean}` : ''}`)
    }
  }, [location])

  useEffect(() => {
    console.log('[Login] isAuthenticated changed:', isAuthenticated)
    if (isAuthenticated) navigate(postLoginRedirectTarget(), { replace: true })
  }, [isAuthenticated, navigate])

  // OTP countdown timer
  useEffect(() => {
    if (otpCountdown <= 0) return
    const timer = setTimeout(() => setOtpCountdown(c => c - 1), 1000)
    return () => clearTimeout(timer)
  }, [otpCountdown])

  // -- Unified identifier submit (username or email) --
  const handleIdentifierContinue = useCallback(async () => {
    if (!identifier) return
    setError(null)
    setSubmitting(true)
    try {
      // Passkey check only works for email identifiers
      if (identifier.includes('@')) {
        const passkeyResult = await getPasskeyLoginOptions(identifier)
        if (passkeyResult.has_passkeys && passkeyResult.options) {
          setHasPasskeys(true)
          setPasskeyOptions(passkeyResult.options)
          setPhase('login')
          return
        }
      }
      setHasPasskeys(false)
      setPhase('login')
    } catch {
      setPhase('login')
    } finally {
      setSubmitting(false)
    }
  }, [identifier])

  // -- Password login/register --
  const handlePasswordSubmit = async () => {
    setError(null)
    setSubmitting(true)
    try {
      if (phase === 'register') {
        // username = name field (what user chose); email is optional capture
        const res = await post<{ access_token: string }>(
          '/auth/password/register',
          {
            username: name,
            name,
            password,
            email: optionalEmail,
          }
        )
        console.log('[Login] register OK — setting auth token')
        setAuthData(res.access_token)
      } else {
        const res = await post<{ access_token: string }>(
          '/auth/password/login',
          { identifier, password }
        )
        console.log('[Login] password login OK — setting auth token')
        setAuthData(res.access_token)
      }
    } catch (err) {
      const status = (err as { response?: { status?: number } }).response?.status
      const msg = err instanceof Error ? err.message : 'Authentication failed'
      if (status && status >= 500) {
        setError('Service temporarily unavailable. Please try again.')
      } else if (msg.toLowerCase().includes('not found') || msg.toLowerCase().includes('no account')) {
        setPhase('register')
        setError(null)
      } else {
        setError(phase === 'login'
          ? "That password doesn't match. Try again or reset it."
          : msg
        )
      }
    } finally {
      setSubmitting(false)
    }
  }

  // -- Passkey login --
  const handlePasskeyLogin = async () => {
    if (!passkeyOptions) return
    setError(null)
    setSubmitting(true)
    try {
      const credential = await navigator.credentials.get({
        publicKey: {
          ...passkeyOptions,
          challenge: Uint8Array.from(atob((passkeyOptions as Record<string, string>).challenge.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0)),
          allowCredentials: ((passkeyOptions as Record<string, unknown[]>).allowCredentials || []).map((c: unknown) => ({
            type: 'public-key' as const,
            id: Uint8Array.from(atob(((c as Record<string, string>).id).replace(/-/g, '+').replace(/_/g, '/')), ch => ch.charCodeAt(0)),
          })),
        },
      })

      if (!credential) {
        setError('Passkey authentication was cancelled.')
        setSubmitting(false)
        return
      }

      // Serialize credential for the server
      const response = (credential as PublicKeyCredential).response as AuthenticatorAssertionResponse
      const serialized = {
        id: credential.id,
        rawId: btoa(String.fromCharCode(...new Uint8Array((credential as PublicKeyCredential).rawId))),
        response: {
          authenticatorData: btoa(String.fromCharCode(...new Uint8Array(response.authenticatorData))),
          clientDataJSON: btoa(String.fromCharCode(...new Uint8Array(response.clientDataJSON))),
          signature: btoa(String.fromCharCode(...new Uint8Array(response.signature))),
          userHandle: response.userHandle ? btoa(String.fromCharCode(...new Uint8Array(response.userHandle))) : null,
        },
        type: credential.type,
      }

      const result = await completePasskeyLogin(
        serialized,
        (passkeyOptions as Record<string, string>)._challenge,
        (passkeyOptions as Record<string, string>)._user_id,
      )
      console.log('[Login] passkey login OK — setting auth token')
      setAuthData(result.access_token)
    } catch {
      setError('Passkey authentication failed. Try your password instead.')
    } finally {
      setSubmitting(false)
    }
  }

  // -- OTP --
  const handleRequestOTP = async () => {
    setSubmitting(true)
    try {
      await requestOTP(identifier)
      setOtpSent(true)
      setOtpCountdown(60)
      setPhase('otp')
      toast.success(`Code sent to ${identifier}`)
    } catch {
      toast.error('Could not send verification code.')
    } finally {
      setSubmitting(false)
    }
  }

  const handleOTPInput = (index: number, value: string) => {
    if (!/^\d*$/.test(value)) return
    const next = [...otpCode]
    next[index] = value.slice(-1)
    setOtpCode(next)

    // Auto-advance
    if (value && index < 5) {
      const nextInput = document.getElementById(`otp-${index + 1}`)
      nextInput?.focus()
    }

    // Auto-submit when all 6 digits entered
    if (value && index === 5 && next.every(d => d)) {
      handleVerifyOTP(next.join(''))
    }
  }

  const handleOTPKeyDown = (index: number, e: React.KeyboardEvent) => {
    if (e.key === 'Backspace' && !otpCode[index] && index > 0) {
      const prevInput = document.getElementById(`otp-${index - 1}`)
      prevInput?.focus()
    }
  }

  const handleVerifyOTP = async (code?: string) => {
    const finalCode = code || otpCode.join('')
    if (finalCode.length !== 6) return
    setSubmitting(true)
    setError(null)
    try {
      const result = await verifyOTP(identifier, finalCode)
      console.log('[Login] OTP verify OK — setting auth token')
      setAuthData(result.access_token)
    } catch {
      setError("That code has expired or isn't correct. We'll send you a new one.")
      setOtpCode(['', '', '', '', '', ''])
      document.getElementById('otp-0')?.focus()
    } finally {
      setSubmitting(false)
    }
  }

  // -- Render helpers --

  const renderIdentifierPhase = () => (
    <>
      <div className="space-y-3">
        <div className="space-y-1.5">
          <input
            id="identifier"
            type="text"
            value={identifier}
            onChange={e => setIdentifier(e.target.value)}
            placeholder="Enter your username"
            className="w-full px-3 py-2.5 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:border-transparent text-sm"
            autoFocus
            autoComplete="username"
            disabled={submitting}
            onKeyDown={e => e.key === 'Enter' && handleIdentifierContinue()}
          />
        </div>
      </div>
      <button
        onClick={handleIdentifierContinue}
        disabled={!identifier || submitting}
        className="w-full py-2.5 bg-gray-900 text-white rounded-lg font-medium hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm mt-4"
      >
        {submitting ? 'Checking...' : 'Continue'}
      </button>
    </>
  )

  const renderLoginPhase = () => (
    <form onSubmit={e => { e.preventDefault(); handlePasswordSubmit() }} className="space-y-4">
      {/* Passkey button (if available) */}
      {hasPasskeys && (
        <button
          type="button"
          onClick={handlePasskeyLogin}
          disabled={submitting}
          className="w-full py-2.5 bg-indigo-600 text-white rounded-lg font-medium hover:bg-indigo-500 disabled:opacity-50 transition-colors text-sm flex items-center justify-center gap-2"
        >
          🔑 Sign in with passkey
        </button>
      )}

      {hasPasskeys && <div className="flex items-center gap-3 text-xs text-gray-400"><div className="flex-1 border-t border-gray-200" /><span>or use your password</span><div className="flex-1 border-t border-gray-200" /></div>}

      {/* Identifier (read-only in this phase) */}
      <div className="flex items-center gap-2 text-sm text-gray-600 bg-gray-50 px-3 py-2 rounded-lg">
        <span>{identifier}</span>
        <button type="button" onClick={() => { setPhase('email'); setPassword(''); setError(null) }} className="text-indigo-500 hover:underline text-xs ml-auto">Change</button>
      </div>

      {/* Password */}
      <div className="space-y-1.5">
        <label htmlFor="password" className="block text-sm font-medium text-gray-700">Password</label>
        <div className="relative">
          <input
            id="password"
            type={showPassword ? 'text' : 'password'}
            value={password}
            onChange={e => setPassword(e.target.value)}
            className="w-full px-3 py-2.5 pr-10 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:border-transparent text-sm"
            autoFocus
            autoComplete="current-password"
            disabled={submitting}
          />
          <button type="button" onClick={() => setShowPassword(!showPassword)} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-sm" tabIndex={-1}>
            {showPassword ? '🙈' : '👁️'}
          </button>
        </div>
        <button type="button" className="text-xs text-indigo-500 hover:underline">Forgot your password?</button>
      </div>

      <button
        type="submit"
        disabled={!password || submitting}
        className="w-full py-2.5 bg-gray-900 text-white rounded-lg font-medium hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm"
      >
        {submitting ? 'Signing in...' : 'Sign in'}
      </button>

      {/* OTP option */}
      {otpEnabled && (
        <button type="button" onClick={handleRequestOTP} disabled={submitting} className="w-full text-sm text-gray-500 hover:text-indigo-600 hover:underline">
          Send me a code instead
        </button>
      )}

      {/* Toggle to register */}
      <button type="button" onClick={() => { setPhase('register'); setError(null) }} className="w-full text-sm text-gray-500 hover:underline">
        No account yet? Create one
      </button>
    </form>
  )

  const renderRegisterPhase = () => (
    <CreateAccountForm
      identifier={identifier}
      onChangeIdentifier={() => { setPhase('email'); setPassword(''); setName(''); setOptionalEmail(''); setError(null) }}
      name={name}
      onNameChange={setName}
      email={optionalEmail}
      onEmailChange={setOptionalEmail}
      password={password}
      onPasswordChange={setPassword}
      submitting={submitting}
      onSubmit={handlePasswordSubmit}
      submitLabel="Create account"
      footer={
        <button type="button" onClick={() => { setPhase('login'); setError(null) }} className="w-full text-sm text-gray-500 hover:underline">
          Already have an account? Sign in
        </button>
      }
    />
  )

  const renderOTPPhase = () => (
    <div className="space-y-6">
      <div className="text-center space-y-2">
        <p className="text-sm text-gray-500">
          We sent a 6-digit code to <span className="font-medium text-gray-700">{identifier}</span>
        </p>
      </div>

      {/* 6-digit OTP input */}
      <div className="flex justify-center gap-2">
        {otpCode.map((digit, i) => (
          <input
            key={i}
            id={`otp-${i}`}
            type="text"
            inputMode="numeric"
            maxLength={1}
            value={digit}
            onChange={e => handleOTPInput(i, e.target.value)}
            onKeyDown={e => handleOTPKeyDown(i, e)}
            autoComplete={i === 0 ? 'one-time-code' : 'off'}
            autoFocus={i === 0}
            className="w-11 h-13 text-center text-xl font-semibold border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:border-transparent"
            disabled={submitting}
          />
        ))}
      </div>

      <button
        onClick={() => handleVerifyOTP()}
        disabled={otpCode.some(d => !d) || submitting}
        className="w-full py-2.5 bg-gray-900 text-white rounded-lg font-medium hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm"
      >
        {submitting ? 'Verifying...' : 'Verify'}
      </button>

      <div className="text-center text-sm">
        {otpCountdown > 0 ? (
          <span className="text-gray-400">Resend in {otpCountdown}s</span>
        ) : (
          <button onClick={handleRequestOTP} disabled={submitting} className="text-indigo-500 hover:underline">
            Didn't get it? Resend
          </button>
        )}
      </div>

      <button type="button" onClick={() => { setPhase('login'); setError(null); setOtpCode(['','','','','','']) }} className="w-full text-sm text-gray-500 hover:underline">
        Use password instead
      </button>
    </div>
  )

  return (
    <AuthLayout
      footer={
        <p className="text-xs text-center text-gray-400 mt-6">
          <Link to="/terms" className="text-indigo-500 hover:underline">Terms</Link>
          {' · '}
          <Link to="/privacy" className="text-indigo-500 hover:underline">Privacy</Link>
        </p>
      }
    >
      {/* Heading */}
      <h1 className="text-center text-lg font-medium text-gray-800 mb-6">
        {phase === 'register' ? 'Create your account' : phase === 'otp' ? 'Enter verification code' : 'Welcome, let’s get you signed in'}
      </h1>

      {/* Error */}
      {error && (
        <div className="mb-4 bg-red-50 border border-red-200 text-red-600 px-4 py-2.5 rounded-lg text-sm" role="alert">
          {error}
        </div>
      )}

      {/* Phase-specific content */}
      {phase === 'email' && renderIdentifierPhase()}
      {phase === 'login' && renderLoginPhase()}
      {phase === 'register' && renderRegisterPhase()}
      {phase === 'otp' && renderOTPPhase()}

      {/* OAuth divider + buttons (only on identifier phase) */}
      {phase === 'email' && providersReady && providers.length > 0 && (
        <>
          <div className="flex items-center gap-3 my-5 text-xs text-gray-400">
            <div className="flex-1 border-t border-gray-200" />
            <span>or sign in with</span>
            <div className="flex-1 border-t border-gray-200" />
          </div>
          <div className="space-y-2">
            {providers.map((p) => (
              <button
                key={p.name}
                onClick={() => login(p.name, setupOperatorToken)}
                className="w-full flex items-center justify-center gap-3 py-2.5 bg-white border border-gray-300 rounded-lg transition-all duration-200 shadow-sm hover:shadow-md hover:border-gray-400 hover:bg-gray-50 active:bg-gray-100 active:scale-[0.98] text-sm"
              >
                {p.name === 'google' ? <FcGoogle className="text-xl" /> : null}
                <span className="font-medium text-gray-600">{p.label}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </AuthLayout>
  )
}

export default Login
