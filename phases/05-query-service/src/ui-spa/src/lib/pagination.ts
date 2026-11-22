/**
 * Accept an opaque continuation token only while it makes forward progress
 * and remains inside the caller's explicit page budget.
 */
export function boundedContinuationToken(
  current: string | null | undefined,
  next: string | null | undefined,
  seen: Set<string>,
  pagesRead: number,
  maxPages: number,
): string | null {
  if (pagesRead >= maxPages || !next || next === current || seen.has(next)) {
    return null;
  }
  seen.add(next);
  return next;
}
