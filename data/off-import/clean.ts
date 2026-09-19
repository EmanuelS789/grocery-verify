/**
 * MODULE: scripts/data-import/off/clean.ts — STEP 2 of the OFF import
 * PURPOSE: Stream the JSONL extract (extract.py) through normalize.ts and
 *          write (1) the load CSV that `\copy` reads into
 *          ref_products_off_staging and (2) report.md — rows in/out,
 *          rejects by reason, duplicate drops, category-map hit rate, size
 *          parse rate, the top 30 unmapped en: tags — the artefact the
 *          grocery-verify /data page publishes beside this source.
 * WHY TWO PASSES: the dump repeats a few codes (3 in the US subset) and the
 *          staging PK must not. Pass 1 records the newest last_modified_t
 *          per canonical barcode (one Map entry per key — ~1.2 M entries fits
 *          comfortably); pass 2 emits only the surviving copy. Memory stays
 *          flat because rows are never held — only the key/timestamp index.
 * DETERMINISTIC: same input file + same categories map → byte-identical
 *          outputs (no timestamps in the report, input order preserved).
 * NO NETWORK: reads two local files, writes two local files. Never talks
 *          to Supabase — the \copy and the swap are Eman's (PROGRESS 🔴
 *          MIGRATION 47 QUEUE / the §3 load steps).
 *
 * RUN (from the repo root):
 *   deno run --config scripts/data-import/deno.json \
 *     --allow-read=scripts/data-import/off/out --allow-write=scripts/data-import/off/out \
 *     scripts/data-import/off/clean.ts \
 *     --in scripts/data-import/off/out/off_us.jsonl \
 *     --categories scripts/data-import/off/out/item_categories.csv \
 *     --out scripts/data-import/off/out/load.csv \
 *     --report scripts/data-import/off/out/report.md
 *
 *   --categories is the target environment's `\copy (select id, name from
 *   public.item_categories) to '…/item_categories.csv' csv header` export;
 *   ids are gen_random_uuid() per project, so DEV and PROD need their own.
 */

import { readJsonLines } from './jsonl.ts';
import {
  cleanRow,
  csvHeader,
  NewestWins,
  parseCategoriesCsv,
  parseExtractRow,
  referenceBarcode,
  toCsvRecord,
} from './normalize.ts';
import type { RejectReason } from './normalize.ts';

// ── args ─────────────────────────────────────────────────────────────────────

interface Args {
  inPath: string;
  outPath: string;
  reportPath: string;
  categoriesPath: string;
}

function parseArgs(argv: readonly string[]): Args {
  const get = (flag: string, fallback: string): string => {
    const i = argv.indexOf(flag);
    const v = i >= 0 ? argv[i + 1] : undefined;
    return v !== undefined ? v : fallback;
  };
  const base = 'scripts/data-import/off/out';
  return {
    inPath: get('--in', `${base}/off_us.jsonl`),
    outPath: get('--out', `${base}/load.csv`),
    reportPath: get('--report', `${base}/report.md`),
    categoriesPath: get('--categories', `${base}/item_categories.csv`),
  };
}

// ── buffered text writer ─────────────────────────────────────────────────────

class LineWriter {
  private readonly encoder = new TextEncoder();
  private chunks: string[] = [];
  private size = 0;
  constructor(private readonly file: Deno.FsFile) {}

  async line(text: string): Promise<void> {
    this.chunks.push(text, '\n');
    this.size += text.length + 1;
    if (this.size > 1 << 20) await this.flush();
  }

  async flush(): Promise<void> {
    if (this.chunks.length === 0) return;
    const bytes = this.encoder.encode(this.chunks.join(''));
    this.chunks = [];
    this.size = 0;
    let written = 0;
    while (written < bytes.length) written += await this.file.write(bytes.subarray(written));
  }
}

// ── report helpers ───────────────────────────────────────────────────────────

function pct(n: number, d: number): string {
  return d === 0 ? 'n/a' : `${((100 * n) / d).toFixed(1)}%`;
}

function table(headers: string[], rows: Array<Array<string | number>>): string {
  const line = (cells: Array<string | number>) => '| ' + cells.map(String).join(' | ') + ' |';
  return [line(headers), '|' + headers.map(() => '---').join('|') + '|', ...rows.map(line)].join('\n');
}

// ── main ─────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const args = parseArgs(Deno.args);
  const categoryIds = parseCategoriesCsv(await Deno.readTextFile(args.categoriesPath));

  // Pass 1 — newest timestamp per canonical barcode.
  const dedupe = new NewestWins();
  let pass1Rows = 0;
  for await (const value of readJsonLines(args.inPath)) {
    pass1Rows += 1;
    const raw = parseExtractRow(value);
    if (raw === null) continue;
    const gate = referenceBarcode(raw.code);
    if ('reject' in gate) continue;
    dedupe.observe(gate.barcode, raw.last_modified_t);
  }

  // Pass 2 — clean, dedupe, write.
  const out = await Deno.open(args.outPath, { write: true, create: true, truncate: true });
  const writer = new LineWriter(out);
  await writer.line(csvHeader());

  const rejects: Record<RejectReason, number> = {
    malformed_row: 0,
    bad_barcode: 0,
    restricted_circulation: 0,
    empty_name: 0,
    name_too_long: 0,
  };
  let rowsIn = 0;
  let rowsOut = 0;
  let duplicatesDropped = 0;
  let usTagged = 0;
  let upcAOnly = 0;
  let hadCategories = 0;
  let categoryMapped = 0;
  let categoryIdResolved = 0;
  let sizeParsed = 0;
  let quantityUnparsed = 0;
  let organic = 0;
  let brandPresent = 0;
  let brandDropped = 0;
  const byLength = new Map<number, number>();
  const unmappedTags = new Map<string, number>();
  const mappedNames = new Map<string, number>();

  for await (const value of readJsonLines(args.inPath)) {
    rowsIn += 1;
    const raw = parseExtractRow(value);
    if (raw === null) {
      rejects.malformed_row += 1;
      continue;
    }
    const result = cleanRow(raw, categoryIds);
    if (result.kind === 'reject') {
      rejects[result.reason] += 1;
      continue;
    }
    if (!dedupe.shouldEmit(result.row.barcode, raw.last_modified_t)) {
      duplicatesDropped += 1;
      continue;
    }
    await writer.line(toCsvRecord(result.row));
    rowsOut += 1;
    if (raw.is_us) usTagged += 1;
    else upcAOnly += 1;
    byLength.set(result.row.barcode.length, (byLength.get(result.row.barcode.length) ?? 0) + 1);
    if (result.hadCategories) hadCategories += 1;
    if (result.categoryMapped) {
      categoryMapped += 1;
      const name = result.row.category_name ?? '';
      mappedNames.set(name, (mappedNames.get(name) ?? 0) + 1);
      if (result.row.category_id !== null) categoryIdResolved += 1;
    } else if (result.hadCategories && raw.categories_tags !== null) {
      for (const tag of raw.categories_tags) {
        if (tag.startsWith('en:')) unmappedTags.set(tag, (unmappedTags.get(tag) ?? 0) + 1);
      }
    }
    if (result.sizeParsed) sizeParsed += 1;
    if (result.quantityUnparsed) quantityUnparsed += 1;
    if (result.row.is_organic === true) organic += 1;
    if (result.row.brand !== null) brandPresent += 1;
    if (result.brandDropped) brandDropped += 1;
  }
  await writer.flush();
  out.close();

  const topUnmapped = [...unmappedTags.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 30);
  const mappedList = [...mappedNames.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const rejectTotal = Object.values(rejects).reduce((a, b) => a + b, 0);

  const report = [
    '# OFF cleaner report',
    '',
    `Input: \`${args.inPath.split('/').pop()}\` (${rowsIn.toLocaleString('en-US')} rows; pass-1 count ${pass1Rows.toLocaleString('en-US')}); categories map: ${categoryIds.size} canonical names → ids. Output: \`${args.outPath.split('/').pop()}\` for \`\\copy public.ref_products_off_staging (${csvHeader()}) FROM … CSV HEADER\`.`,
    '',
    'Every decision below is the app\'s own code: `normalizeScannedBarcode` (gate + canonical form), the proxy\'s `mapOffProduct` (name / brand / categories / size / organic — identical to a live answer), `mapOffCategoriesToCanonical` (category). Deterministic — same inputs, same bytes.',
    '',
    '## 1. Rows',
    '',
    table(['metric', 'rows', '% of input'], [
      ['rows in', rowsIn.toLocaleString('en-US'), '100%'],
      ['**rows out (loadable)**', `**${rowsOut.toLocaleString('en-US')}**`, pct(rowsOut, rowsIn)],
      ['rejected', rejectTotal.toLocaleString('en-US'), pct(rejectTotal, rowsIn)],
      ['duplicate copies dropped (newest `last_modified_t` kept)', duplicatesDropped.toLocaleString('en-US'), pct(duplicatesDropped, rowsIn)],
      ['of rows out: tagged `en:united-states`', usTagged.toLocaleString('en-US'), pct(usTagged, rowsOut)],
      ['of rows out: UPC-A-shaped but not US-tagged (filter (c))', upcAOnly.toLocaleString('en-US'), pct(upcAOnly, rowsOut)],
    ]),
    '',
    '## 2. Rejects by reason',
    '',
    table(['reason', 'rows', 'what it means'], [
      ['bad_barcode', rejects.bad_barcode.toLocaleString('en-US'), 'fails the scanned-payload gate: not 8 / 12 / 13 / 14 digits after the fold, or a bad GS1 check digit (zero-padded PLUs, OFF-internal ids, misreads)'],
      ['restricted_circulation', rejects.restricted_circulation.toLocaleString('en-US'), 'a number-system-2 code (GS1 prefix 20–29): store-assigned, variable-weight / in-store — never globally unique, so never a reference row (standing rule, 2026-09-16)'],
      ['empty_name', rejects.empty_name.toLocaleString('en-US'), 'no `main` and no `en` product name — nothing to show a user'],
      ['name_too_long', rejects.name_too_long.toLocaleString('en-US'), 'name over the 500-char DB CHECK'],
      ['malformed_row', rejects.malformed_row.toLocaleString('en-US'), 'unparseable JSON line or missing code / timestamp'],
    ]),
    '',
    'Size is never a reject: the proxy omits a size it cannot parse (or that merely echoes the serving size) and the user types it — the same row is still an identity. See §4.',
    '',
    '## 3. Canonical barcode form of rows out',
    '',
    table(['length', 'rows', 'form'], [...byLength.entries()].sort((a, b) => a[0] - b[0]).map(([len, n]) => [
      len,
      n.toLocaleString('en-US'),
      len === 12 ? 'UPC-A (the dump\'s 13-digit `0…` folded)' : len === 13 ? 'EAN-13' : len === 8 ? 'EAN-8 / UPC-E' : 'GTIN-14',
    ])),
    '',
    '## 4. Field fill on rows out',
    '',
    table(['field', 'rows', '% of rows out'], [
      ['brand', brandPresent.toLocaleString('en-US'), pct(brandPresent, rowsOut)],
      ['brand dropped (over 500 chars)', brandDropped.toLocaleString('en-US'), pct(brandDropped, rowsOut)],
      ['size parsed (value + unit)', sizeParsed.toLocaleString('en-US'), pct(sizeParsed, rowsOut)],
      ['quantity present but no size (multipack / unmappable unit / serving echo)', quantityUnparsed.toLocaleString('en-US'), pct(quantityUnparsed, rowsOut)],
      ['is_organic = true (`en:usda-organic` only)', organic.toLocaleString('en-US'), pct(organic, rowsOut)],
      ['categories present (proxy produced ≥ 1 category string)', hadCategories.toLocaleString('en-US'), pct(hadCategories, rowsOut)],
      ['**category mapped to a canonical name**', `**${categoryMapped.toLocaleString('en-US')}**`, `${pct(categoryMapped, rowsOut)} of rows out · ${pct(categoryMapped, hadCategories)} of rows with categories`],
      ['category_id resolved through the id map', categoryIdResolved.toLocaleString('en-US'), pct(categoryIdResolved, categoryMapped)],
    ]),
    '',
    '## 5. Mapped canonical categories',
    '',
    table(['canonical category', 'rows'], mappedList.map(([name, n]) => [name, n.toLocaleString('en-US')])),
    '',
    '## 6. Top 30 unmapped `en:` category tags (rows with categories but no canonical match)',
    '',
    table(['tag', 'rows'], topUnmapped.map(([tag, n]) => [tag, n.toLocaleString('en-US')])),
    '',
  ].join('\n');

  await Deno.writeTextFile(args.reportPath, report);
  console.log(`clean: ${rowsIn.toLocaleString('en-US')} in → ${rowsOut.toLocaleString('en-US')} out (${rejectTotal.toLocaleString('en-US')} rejected, ${duplicatesDropped.toLocaleString('en-US')} duplicate copies dropped) → ${args.outPath}; report → ${args.reportPath}`);
}

if (import.meta.main) {
  await main();
}
