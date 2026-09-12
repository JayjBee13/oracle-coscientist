/**
 * Context documents: the files a scientist drops in so the run knows what they
 * already know — a brief, a data dictionary, last quarter's findings.
 *
 * The bytes are read here and travel inline in `POST /runs` and
 * `POST /workshops`. Nothing ever hands the model a *path*: the engine has no
 * filesystem access by design, and that is not negotiable for a convenience.
 */

import type { ContextDocInput } from "../../api/types";

export const MAX_DOC_BYTES = 200 * 1024;
export const MAX_DOCS = 5;
export const ACCEPTED_EXTENSIONS = [".md", ".txt", ".csv", ".json"] as const;
export const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.join(",");

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Characters, which is what the backend stores and the Settings tab shows. */
export function docChars(doc: ContextDocInput): number {
  return doc.text.length;
}

function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

export type ReadDocsResult = {
  docs: ContextDocInput[];
  /** One sentence per rejected file, named — never a silent drop. */
  errors: string[];
};

/**
 * Validates and reads files, appending to what is already attached.
 *
 * Every rejection is reported with the file's name and the reason: too big,
 * wrong kind, already attached, or over the limit. A file that vanishes
 * between the drop and the read reports that too.
 */
export async function readContextDocs(
  files: readonly File[],
  existing: readonly ContextDocInput[],
): Promise<ReadDocsResult> {
  const docs: ContextDocInput[] = [];
  const errors: string[] = [];
  const taken = new Set(existing.map((doc) => doc.name));

  for (const file of files) {
    if (existing.length + docs.length >= MAX_DOCS) {
      errors.push(
        `${file.name} was not added — ${MAX_DOCS} documents is the limit for one run.`,
      );
      continue;
    }
    if (!hasAcceptedExtension(file.name)) {
      errors.push(
        `${file.name} is not a kind this reads. Use ${ACCEPTED_EXTENSIONS.join(", ")}.`,
      );
      continue;
    }
    if (file.size > MAX_DOC_BYTES) {
      errors.push(
        `${file.name} is ${formatBytes(file.size)} — the limit is ${formatBytes(MAX_DOC_BYTES)} per document.`,
      );
      continue;
    }
    if (taken.has(file.name)) {
      errors.push(`${file.name} is already attached.`);
      continue;
    }

    try {
      const text = await file.text();
      if (!text.trim()) {
        errors.push(`${file.name} is empty.`);
        continue;
      }
      taken.add(file.name);
      docs.push({ name: file.name, text });
    } catch {
      errors.push(`${file.name} could not be read.`);
    }
  }

  return { docs, errors };
}
