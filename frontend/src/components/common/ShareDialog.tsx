import { useState, useCallback } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { toast } from 'sonner';
import { post } from '@/api/api';

interface ShareDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  workspaceName?: string;
}

const ROLES = [
  { value: 'viewer', label: 'Viewer' },
  { value: 'editor', label: 'Editor' },
  { value: 'collaborator', label: 'Collaborator' },
  { value: 'admin', label: 'Admin' },
];

// Role -> CRUDIASO bit bundle. Must match backend Grant.ROLE_PRESETS.
const ROLE_BITS: Record<string, Record<string, boolean>> = {
  viewer: { can_read: true },
  editor: { can_create: true, can_read: true, can_update: true, can_delete: true },
  collaborator: {
    can_create: true,
    can_read: true,
    can_update: true,
    can_delete: true,
    can_invoke: true,
    can_add: true,
    can_share: true,
  },
  admin: {
    can_create: true,
    can_read: true,
    can_update: true,
    can_delete: true,
    can_invoke: true,
    can_add: true,
    can_share: true,
    can_admin: true,
  },
};

export function ShareDialog({ open, onOpenChange, workspaceId, workspaceName }: ShareDialogProps) {
  const [email, setEmail] = useState('');
  const [role, setRole] = useState('viewer');
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);

  const deepLink = `${window.location.origin}/${workspaceId}`;

  const handleCopyLink = useCallback(() => {
    navigator.clipboard.writeText(deepLink).then(
      () => toast.success('Link copied to clipboard'),
      () => toast.error('Failed to copy link'),
    );
  }, [deepLink]);

  const handleInvite = useCallback(async () => {
    if (!email.trim()) {
      toast.error('Please enter an email address');
      return;
    }
    setSending(true);
    try {
      await post('/grants', {
        resource_id: workspaceId,
        resource_type: 'collection',
        grantee_type: 'invite',
        target_entity: email.trim().toLowerCase(),
        target_entity_type: 'email',
        max_claims: 1,
        // Named role is the source of truth; the backend derives CRUDIASO
        // bits from Grant.ROLE_PRESETS.
        role,
        message: message.trim() || null,
        // CRUDIASO bits kept for any legacy server that doesn't support
        // the `role` parameter yet.
        ...ROLE_BITS[role],
      });
      toast.success(`Invite sent to ${email}`);
      setEmail('');
      setMessage('');
      onOpenChange(false);
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        'Failed to create invite';
      toast.error(detail);
    } finally {
      setSending(false);
    }
  }, [email, role, workspaceId, message, onOpenChange]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Share {workspaceName || 'workspace'}</DialogTitle>
          <DialogDescription>Invite someone by email or copy a link.</DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-3">
            <label className="text-sm font-medium text-gray-700">Invite by email</label>
            <div className="flex gap-2">
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@example.com"
                className="flex-1 px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-gray-900"
                onKeyDown={(e) => e.key === 'Enter' && handleInvite()}
              />
              <select
                value={role}
                onChange={(e) => setRole(e.target.value)}
                className="px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-gray-900"
              >
                {ROLES.map((r) => (
                  <option key={r.value} value={r.value}>{r.label}</option>
                ))}
              </select>
            </div>
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="Add a message (optional)"
              rows={2}
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-gray-900 resize-none"
            />
            <button
              onClick={handleInvite}
              disabled={sending || !email.trim()}
              className="w-full bg-gray-900 text-white py-2 px-4 rounded-md hover:bg-gray-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed text-sm"
            >
              {sending ? 'Sending...' : 'Send invite'}
            </button>
          </div>

          <div className="relative">
            <div className="absolute inset-0 flex items-center">
              <span className="w-full border-t border-gray-200" />
            </div>
            <div className="relative flex justify-center text-xs uppercase">
              <span className="bg-white px-2 text-gray-500">or</span>
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium text-gray-700">Copy link</label>
            <p className="text-xs text-gray-500">Anyone with the link can navigate here, but only invited users can access content.</p>
            <div className="flex gap-2">
              <input
                type="text"
                value={deepLink}
                readOnly
                className="flex-1 px-3 py-2 text-sm bg-gray-50 border border-gray-200 rounded-md text-gray-600"
              />
              <button
                onClick={handleCopyLink}
                className="px-4 py-2 text-sm border border-gray-300 rounded-md hover:bg-gray-50 transition-colors"
              >
                Copy
              </button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
