/**
 * MODULE: scripts/data-import/off/jsonl.ts
 * PURPOSE: Stream a newline-delimited JSON file one line at a time without
 *          ever holding the whole file in memory (the full OFF extract is
 *          ~1.2 M rows / a few hundred MB).
 * WHY HAND-ROLLED: a line splitter over TextDecoderStream is ~20 lines and
 *          keeps the cleaner dependency-free (no jsr/npm fetch on first run —
 *          the data files are the only thing that ever leaves the machine's
 *          disk during an import). Blank lines are skipped; a trailing '\r'
 *          is tolerated so a CRLF-flipped file still parses.
 */

/** Yields each non-empty line of `path`, in order. */
export async function* readLines(path: string): AsyncGenerator<string, void, undefined> {
  const file = await Deno.open(path, { read: true });
  try {
    const reader = file.readable.pipeThrough(new TextDecoderStream()).getReader();
    let carry = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = carry + value;
      const parts = chunk.split('\n');
      carry = parts.pop() ?? '';
      for (const part of parts) {
        const line = part.endsWith('\r') ? part.slice(0, -1) : part;
        if (line.length > 0) yield line;
      }
    }
    if (carry.length > 0) {
      const line = carry.endsWith('\r') ? carry.slice(0, -1) : carry;
      if (line.length > 0) yield line;
    }
  } finally {
    // The reader owns the file once piped; closing the reader releases it.
    // (Deno.open's resource is consumed by .readable — no explicit close.)
  }
}

/** Yields each line parsed as JSON. A malformed line yields `undefined`
 *  (the caller counts it as a reject rather than aborting the run). */
export async function* readJsonLines(path: string): AsyncGenerator<unknown, void, undefined> {
  for await (const line of readLines(path)) {
    try {
      yield JSON.parse(line) as unknown;
    } catch {
      yield undefined;
    }
  }
}
