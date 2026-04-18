import { useState, useEffect, useMemo } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog';
import { FolderOpen, Search } from 'lucide-react';
import { listCollections } from '../../api/collections';
import { CollectionResponse } from '../../api/types';
import toast from 'react-hot-toast';

interface BindingPickerProps {
  open: boolean;
  onClose: () => void;
  /** Called when the user picks an artifact. */
  onSelect: (artifactId: string) => void;
  /** Role label shown in the dialog title. */
  label?: string;
}

export function BindingPicker({ open, onClose, onSelect, label }: BindingPickerProps) {
  const [collections, setCollections] = useState<CollectionResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    if (open) {
      loadCollections();
      setSearchQuery('');
    }
  }, [open]);

  const loadCollections = async () => {
    try {
      setLoading(true);
      const data = await listCollections();
      setCollections(data);
    } catch (error) {
      console.error('Failed to load collections:', error);
      toast.error('Failed to load collections');
    } finally {
      setLoading(false);
    }
  };

  const filtered = useMemo(
    () =>
      collections.filter((c) =>
        (c.name ?? '').toLowerCase().includes(searchQuery.toLowerCase()),
      ),
    [collections, searchQuery],
  );

  const handleSelect = (id: string) => {
    onSelect(id);
    onClose();
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{label ? `Select: ${label}` : 'Select Artifact'}</DialogTitle>
        </DialogHeader>

        <div className="relative mb-3">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search collections…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full rounded-md border bg-background py-2 pl-9 pr-3 text-sm outline-none focus:ring-1 focus:ring-ring"
            autoFocus
          />
        </div>

        <div className="max-h-64 overflow-y-auto space-y-1">
          {loading ? (
            <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>
          ) : filtered.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">No results</p>
          ) : (
            filtered.map((c) => (
              <button
                key={c.id}
                onClick={() => handleSelect(c.id)}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent text-left"
              >
                <FolderOpen className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="truncate">{c.name || c.id}</span>
              </button>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
