/**
 * Post-login redirect target.
 *
 * If an invite token is stashed in sessionStorage (by the claim page when
 * an unauthenticated user landed there), the user came here to accept an
 * invite --- send them back to the claim page so it can finish the job.
 *
 * Otherwise: home.
 */
const PENDING_INVITE_KEY = 'pending_invite_token';

export function popPendingInviteTarget(): string | null {
  try {
    const token = sessionStorage.getItem(PENDING_INVITE_KEY);
    if (!token) return null;
    // Claim page removes the key itself on successful claim; if the user
    // arrives via a fresh login flow, leave it so the page can auto-claim.
    return `/invite/${encodeURIComponent(token)}`;
  } catch {
    return null;
  }
}

/**
 * Redirect target for a newly-authenticated user.
 * Falls back to ``/`` (home) when no pending invite.
 */
export function postLoginRedirectTarget(): string {
  return popPendingInviteTarget() ?? '/';
}
