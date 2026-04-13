/**
 * Centralized, product-level UI copy.
 *
 * Goals:
 * - Keep internal identifiers stable (e.g., API/entities still use "artifact").
 * - Allow user-facing terminology to evolve ("Artifact" -> "Note") from one place.
 * - Provide a home for glossary/help/tooltips without scattering strings.
 */

export type ProductNoun = {
  singular: string;
  plural: string;
};

export const PRODUCT_NOUN: ProductNoun = {
  singular: 'Artifact',
  plural: 'Artifacts',
} as const;

export function noun(count: number): string {
  return count === 1 ? PRODUCT_NOUN.singular : PRODUCT_NOUN.plural;
}

export function indefinite(nounSingular: string): string {
  // Minimal heuristic; good enough for UI copy.
  const first = (nounSingular || '').trim().charAt(0).toLowerCase();
  const article = ['a', 'e', 'i', 'o', 'u'].includes(first) ? 'an' : 'a';
  return `${article} ${nounSingular}`;
}

export const GLOSSARY = {
  note: {
    term: 'Note',
    aka: ['Artifact', 'Unit', 'Item'],
    definition:
      'A Note is the basic knowledge object in Agience. Notes can represent decisions, actions, constraints, or claims. Sources and evidence can be attached as optional metadata when useful.',
  },
  workspace: {
    term: 'Workspace',
    definition:
      'A private draft area for ingesting and curating Notes before promoting them to durable truth.',
  },
  collection: {
    term: 'Collection',
    definition:
      'A durable, versioned library of committed Notes.',
  },
  commit: {
    term: 'Commit',
    definition:
      'The explicit promotion step that moves curated Notes from a Workspace into a Collection.',
  },
  source: {
    term: 'Source',
    definition:
      'A transcript or artifact a Note was derived from. Stored as metadata so it can be searched and optionally enforced later.',
  },
  evidence: {
    term: 'Evidence',
    definition:
      'Optional quoted spans or locators that support a Note and link it back to a source transcript or artifact.',
  },
  namespacing: {
    term: 'Namespacing',
    definition:
      'A future convention for organizing metadata keys (and enabling privacy) to avoid collisions in Note context.',
  },
} as const;

export const TOOLTIPS = {
  note: {
    evidence:
      'Evidence is optional metadata that links a Note back to source text or an artifact.',
    source:
      'Source metadata points back to the transcript or artifact this Note came from.',
  },
  commit: {
    preview:
      'Preview what will be added/updated/removed before committing to a collection.',
  },
} as const;
