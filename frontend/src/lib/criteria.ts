/**
 * The acceptance criteria the server keeps, mirrored on the client.
 *
 * The API keeps the first `MAX_CRITERION` characters of each criterion, so the
 * publish screen has to compare the same shape it will get back. Comparing the raw
 * draft against the stored list never settles for a longer line, which left the
 * "commit funds" button disabled with nothing on screen saying why.
 */

export const MAX_CRITERION = 500
export const MAX_CRITERIA = 12

/** One criterion per non-empty line, trimmed, as the textarea and the API both read it. */
export function criteriaLines(draft: string): string[] {
  return draft
    .split('\n')
    .map((c) => c.trim())
    .filter(Boolean)
}

/** What the server stores after trimming: the first MAX_CRITERION characters of each. */
export function asStored(lines: string[]): string {
  return lines.map((c) => c.slice(0, MAX_CRITERION)).join('\n')
}

/** Whether any criterion is longer than the server will keep. */
export function tooLong(lines: string[]): boolean {
  return lines.some((c) => c.length > MAX_CRITERION)
}

/** Whether the draft is what the issue already has, so nothing needs approving. */
export function sameAsStored(draftLines: string[], stored: string[]): boolean {
  return asStored(draftLines) === asStored(stored)
}
