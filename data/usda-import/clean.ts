/**
 * MODULE: scripts/data-import/usda/clean.ts — STEP 2 of the USDA import
 * PURPOSE: Stream the JSONL extract (extract.py) through normalize.ts and
 *          write (1) the load CSV that `\copy` reads into
 *          ref_products_usda_staging and (2) report.md — rows in/out,
 *          rejects by reason, the canonical-form split (with the 14-digit
 *          count that MUST be 0), brand / casing / size / category /
 *          store-brand coverage, the mapped-category distribution, the top
 *          30 unmapped categories, the owner-list hits — the artefact the
 *          grocery-verify /data page publishes beside this source.
 * WHY TWO PASSES: the same product appears under several gtin_upc SPELLINGS
 *          (12 / 0…13 / 00…14 — 20,178 collisions on the 2026-04-30 kept set)
 *          and the staging PK must not. Pass 1 records the newest
 *          publication_date per CANONICAL barcode (one Map entry per key);
 *          pass 2 emits only the surviving copy (ties → first seen in the
 *          extract's ORDER BY gtin — deterministic).
 * DETERMINISTIC: same input file + same three input maps → byte-identical
 *          outputs (no timestamps in the report, input order preserved).
 * NO NETWORK: reads four local files, writes two local files. Never talks
 *          to Supabase — the \copy and the swap are Eman's (PROGRESS 🔴
 *          queues).
 *
 * RUN (from the repo root):
 *   deno run --config scripts/data-import/deno.json \
 *     --allow-read=scripts/data-import/usda --allow-write=scripts/data-import/usda/out \
 *     scripts/data-import/usda/clean.ts \
 *     --in scripts/data-import/usda/out/usda_us.jsonl \
 *     --categories scripts/data-import/usda/out/item_categories.csv \
 *     --map scripts/data-import/usda/usda_category_map.csv \
 *     --owners scripts/data-import/usda/usda_retailer_owners.csv \
 *     --out scripts/data-import/usda/out/load.csv \
 *     --report scripts/data-import/usda/out/report.md
 *
 *   --categories is the TARGET ENVIRONMENT'S `\copy (select id, name from
 *   public.item_categories) to '…/item_categories.csv' csv header` export;
 *   ids are gen_random_uuid() per project, so DEV and PROD need their own.
 */

import { readJsonLines } from '../off/jsonl.ts';
import { NewestWins, parseCategoriesCsv } from '../off/normalize.ts';
import {
  cleanRow,
  csvHeader,
  dateToEpoch,
  parseCategoryMapBlanks,
  parseCategoryMapCsv,
  parseExtractRow,
  parseOwnerListCsv,
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
  mapPath: string;
  ownersPath: string;
}

function parseArgs(argv: readonly string[]): Args {
  const get = (flag: string, fallback: string): string => {
    const i = argv.indexOf(flag);
    const v = i >= 0 ? argv[i + 1] : undefined;
    return v !== undefined ? v : fallback;
  };
  const base = 'scripts/data-import/usda';
  return {
    inPath: get('--in', `${base}/out/usda_us.jsonl`),
    outPath: get('--out', `${base}/out/load.csv`),
    reportPath: get('--report', `${base}/out/report.md`),
    categoriesPath: get('--categories', `${base}/out/item_categories.csv`),
    mapPath: get('--map', `${base}/usda_category_map.csv`),
    ownersPath: get('--owners', `${base}/usda_retailer_owners.csv`),
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

function fmt(n: number): string {
  return n.toLocaleString('en-US');
}

function table(headers: string[], rows: Array<Array<string | number>>): string {
  const line = (cells: Array<string | number>) => '| ' + cells.map(String).join(' | ') + ' |';
  return [line(headers), '|' + headers.map(() => '---').join('|') + '|', ...rows.map(line)].join('\n');
}

function bump(map: Map<string, number>, key: string): void {
  map.set(key, (map.get(key) ?? 0) + 1);
}

function sorted(map: Map<string, number>): Array<[string, number]> {
  return [...map.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

// ── main ─────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const args = parseArgs(Deno.args);
  const categoryIds = parseCategoriesCsv(await Deno.readTextFile(args.categoriesPath));
  const mapText = await Deno.readTextFile(args.mapPath);
  const categoryMap = parseCategoryMapCsv(mapText);
  const mapBlanks = parseCategoryMapBlanks(mapText);
  const owners = parseOwnerListCsv(await Deno.readTextFile(args.ownersPath));
  const inputs = { categoryMap, categoryIds, owners };

  // Every canonical name the map points at must exist in the target
  // environment — a missing one means the wrong environment's export.
  const missing = [...new Set(categoryMap.values())].filter((c) => !categoryIds.has(c));
  if (missing.length > 0) {
    throw new Error(`the category map names canonical categories the id export lacks: ${missing.join(' | ')}`);
  }

  // Pass 1 — newest publication per canonical barcode.
  const dedupe = new NewestWins();
  let pass1Rows = 0;
  for await (const value of readJsonLines(args.inPath)) {
    pass1Rows += 1;
    const raw = parseExtractRow(value);
    if (raw === null) continue;
    const gate = referenceBarcode(raw.gtin);
    if ('reject' in gate) continue;
    dedupe.observe(gate.barcode, dateToEpoch(raw.publication_date));
  }

  // Pass 2 — clean, dedupe, write.
  const out = await Deno.open(args.outPath, { write: true, create: true, truncate: true });
  const writer = new LineWriter(out);
  await writer.line(csvHeader());

  const rejects: Record<RejectReason, number> = {
    malformed_row: 0,
    case_code: 0,
    bad_barcode: 0,
    restricted_circulation: 0,
    empty_name: 0,
    name_too_long: 0,
  };
  let rowsIn = 0;
  let rowsOut = 0;
  let duplicatesDropped = 0;
  let preFolded = 0;
  let whitespaceStripped = 0;
  let usStrict = 0;
  let brandPresent = 0;
  let brandFromOwner = 0;
  let brandPlaceholder = 0;
  let brandDropped = 0;
  let titleCased = 0;
  let sizeParsed = 0;
  let weightUnparsed = 0;
  let hadCategory = 0;
  let categoryMapped = 0;
  let categoryIdResolved = 0;
  let storeBrand = 0;
  let storeBrandByApp = 0;
  let storeBrandByOwner = 0;
  let storeBrandOwnerOnly = 0;
  const byLength = new Map<number, number>();
  const mappedNames = new Map<string, number>();
  const unmappedCategories = new Map<string, number>();
  const blankByDecision = new Map<string, number>();
  const ownerHits = new Map<string, number>();
  const units = new Map<string, number>();

  for await (const value of readJsonLines(args.inPath)) {
    rowsIn += 1;
    const raw = parseExtractRow(value);
    if (raw === null) {
      rejects.malformed_row += 1;
      continue;
    }
    const result = cleanRow(raw, inputs);
    if (result.kind === 'reject') {
      rejects[result.reason] += 1;
      continue;
    }
    if (!dedupe.shouldEmit(result.row.barcode, dateToEpoch(raw.publication_date))) {
      duplicatesDropped += 1;
      continue;
    }
    await writer.line(toCsvRecord(result.row));
    rowsOut += 1;
    if (result.preFolded) preFolded += 1;
    if (result.whitespaceStripped) whitespaceStripped += 1;
    if (raw.market_country === 'United States') usStrict += 1;
    byLength.set(result.row.barcode.length, (byLength.get(result.row.barcode.length) ?? 0) + 1);
    if (result.row.brand !== null) brandPresent += 1;
    if (result.brandFromOwner) brandFromOwner += 1;
    if (result.brandPlaceholder) brandPlaceholder += 1;
    if (result.brandDropped) brandDropped += 1;
    if (result.titleCased) titleCased += 1;
    if (result.sizeParsed) {
      sizeParsed += 1;
      bump(units, result.row.size_unit ?? '');
    }
    if (result.weightUnparsed) weightUnparsed += 1;
    if (result.hadCategory) hadCategory += 1;
    if (result.categoryMapped) {
      categoryMapped += 1;
      bump(mappedNames, result.row.category_name ?? '');
      if (result.row.category_id !== null) categoryIdResolved += 1;
    } else if (result.hadCategory) {
      const cat = result.row.usda_category ?? '';
      if (mapBlanks.has(cat)) bump(blankByDecision, cat);
      else bump(unmappedCategories, cat);
    }
    if (result.row.is_store_brand) storeBrand += 1;
    if (result.storeBrandByApp) storeBrandByApp += 1;
    if (result.storeBrandByOwner) {
      storeBrandByOwner += 1;
      if (!result.storeBrandByApp) storeBrandOwnerOnly += 1;
      const hay = (result.row.brand_owner ?? '').toLowerCase();
      for (const o of owners) if (hay.includes(o)) bump(ownerHits, o);
    }
  }
  await writer.flush();
  out.close();

  const rejectTotal = Object.values(rejects).reduce((a, b) => a + b, 0);
  const fourteen = byLength.get(14) ?? 0;

  const report = [
    '# USDA cleaner report',
    '',
    `Input: \`${args.inPath.split('/').pop()}\` (${fmt(rowsIn)} rows; pass-1 count ${fmt(pass1Rows)}); category map: ${categoryMap.size} USDA categories → a canonical name; id map: ${categoryIds.size} canonical names → ids; owner list: ${owners.length} retailer strings. Output: \`${args.outPath.split('/').pop()}\` for \`\\copy public.ref_products_usda_staging (${csvHeader()}) FROM … CSV HEADER\`.`,
    '',
    'Every decision below is the app\'s own code or a committed, reviewed input: the indicator-0 pre-fold then `normalizeScannedBarcode` (gate + canonical form), `hasRestrictedCirculationPrefix` (prefix-2), the proxy\'s `mapOffProduct` size grammar on the FIRST slash token of `package_weight`, `classifyStoreBrand` OR `usda_retailer_owners.csv` (store brand), `usda_category_map.csv` (category). Names: title-cased only when the source is all-caps; brand = `brand_name` unless empty / `N/A`, else `brand_owner`. Deterministic — same inputs, same bytes.',
    '',
    '## 1. Rows',
    '',
    table(['metric', 'rows', '% of input'], [
      ['rows in', fmt(rowsIn), '100%'],
      ['**rows out (loadable)**', `**${fmt(rowsOut)}**`, pct(rowsOut, rowsIn)],
      ['rejected', fmt(rejectTotal), pct(rejectTotal, rowsIn)],
      ['duplicate spellings dropped (newest `publication_date` per canonical barcode kept)', fmt(duplicatesDropped), pct(duplicatesDropped, rowsIn)],
      ['of rows out: `market_country = \'United States\'` (the rest spelled `US`)', fmt(usStrict), pct(usStrict, rowsOut)],
      ['of rows out: pre-folded from a 14-digit indicator-0 spelling', fmt(preFolded), pct(preFolded, rowsOut)],
      ['of rows out: raw spelling carried whitespace (stripped; `gtin_upc_raw` stores the digits)', fmt(whitespaceStripped), pct(whitespaceStripped, rowsOut)],
    ]),
    '',
    '## 2. Rejects by reason',
    '',
    table(['reason', 'rows', 'what it means'], [
      ['case_code', fmt(rejects.case_code), '14 digits with a NON-ZERO packaging indicator — a case / logistics code, never a retail identity'],
      ['bad_barcode', fmt(rejects.bad_barcode), 'fails the scanned-payload gate after the pre-fold: not 8 / 12 / 13 / 14 digits, or a bad GS1 check digit (11-digit exports, misreads, non-digits)'],
      ['restricted_circulation', fmt(rejects.restricted_circulation), 'a number-system-2 code (GS1 prefix 20–29): store-assigned, variable-weight / in-store — never globally unique, so never a reference row (standing decision #11)'],
      ['empty_name', fmt(rejects.empty_name), 'no `description` — nothing to show a user'],
      ['name_too_long', fmt(rejects.name_too_long), 'name over the 500-char DB CHECK'],
      ['malformed_row', fmt(rejects.malformed_row), 'unparseable JSON line, or a missing / non-positive fdc_id or a bad publication_date'],
    ]),
    '',
    'Size is never a reject: an unparsed label leaves the size pair NULL and the user types it — the row is still an identity.',
    '',
    '## 3. Canonical barcode form of rows out',
    '',
    table(['length', 'rows', 'form'], [...byLength.entries()].sort((a, b) => a[0] - b[0]).map(([len, n]) => [
      len,
      fmt(n),
      len === 12 ? 'UPC-A (12 as written, or the `0…`13 / `00…`14 spellings folded)' : len === 13 ? 'EAN-13 (non-zero lead; incl. a `0`-indicator GTIN-14 body)' : len === 8 ? 'EAN-8 / UPC-E' : 'GTIN-14 — MUST BE 0 (a retail GTIN-14 cannot survive the pre-fold)',
    ])),
    '',
    `**14-digit rows out: ${fmt(fourteen)}** — ${fourteen === 0 ? 'as required (the DB CHECK would accept them; the cleaner is the enforcement)' : '⚠️ NOT ZERO — do not load; the pre-fold is broken'}.`,
    '',
    '## 4. Field fill on rows out',
    '',
    table(['field', 'rows', '% of rows out'], [
      ['brand (after the `brand_name` → `brand_owner` rule)', fmt(brandPresent), pct(brandPresent, rowsOut)],
      ['…brand taken from `brand_owner` (brand_name empty or a placeholder)', fmt(brandFromOwner), pct(brandFromOwner, rowsOut)],
      ['…brand_name was a placeholder literal (`N/A` / `NA`)', fmt(brandPlaceholder), pct(brandPlaceholder, rowsOut)],
      ['brand / owner dropped (over 500 chars)', fmt(brandDropped), pct(brandDropped, rowsOut)],
      ['name title-cased (source was all-caps)', fmt(titleCased), pct(titleCased, rowsOut)],
      ['size parsed (first `package_weight` token through the app grammar)', fmt(sizeParsed), pct(sizeParsed, rowsOut)],
      ['package_weight present but no size (spelling noise: `ONZ`, `0Z`, `LBR`, fractions, multipacks)', fmt(weightUnparsed), pct(weightUnparsed, rowsOut)],
      ['usda_category present', fmt(hadCategory), pct(hadCategory, rowsOut)],
      ['**category mapped to a canonical name (map hit)**', `**${fmt(categoryMapped)}**`, `${pct(categoryMapped, rowsOut)} of rows out · ${pct(categoryMapped, hadCategory)} of rows with a category`],
      ['category_id resolved through the id map', fmt(categoryIdResolved), pct(categoryIdResolved, categoryMapped)],
      ['**is_store_brand = true**', `**${fmt(storeBrand)}**`, pct(storeBrand, rowsOut)],
      ['…by the app\'s `classifyStoreBrand` (name + brand)', fmt(storeBrandByApp), pct(storeBrandByApp, rowsOut)],
      ['…by the owner list (`brand_owner` contains a retailer string)', fmt(storeBrandByOwner), pct(storeBrandByOwner, rowsOut)],
      ['…by the owner list ONLY (the classifier gap the flag closes)', fmt(storeBrandOwnerOnly), pct(storeBrandOwnerOnly, rowsOut)],
    ]),
    '',
    'Units produced by the size parse: ' + (sorted(units).map(([u, n]) => `\`${u}\` ${fmt(n)}`).join(', ') || 'none') + '.',
    '',
    '## 5. Mapped canonical categories',
    '',
    table(['canonical category', 'rows'], sorted(mappedNames).map(([name, n]) => [name, fmt(n)])),
    '',
    '## 6. Top 30 unmapped USDA categories (rows whose category is NOT IN THE MAP AT ALL — a vocabulary the map has never seen)',
    '',
    table(['usda_category', 'rows'], sorted(unmappedCategories).slice(0, 30).map(([c, n]) => [c, fmt(n)])),
    '',
    'Categories the map lists with a blank canonical ON PURPOSE (a recorded decision, not a gap; the rows load with `category_id` NULL): ' + (sorted(blankByDecision).map(([c, n]) => `${c} ${fmt(n)}`).join(', ') || 'none') + '.',
    '',
    '## 7. Owner-list hits (rows flagged store-brand through `brand_owner`; a row can hit more than one string)',
    '',
    table(['owner string', 'rows'], sorted(ownerHits).map(([o, n]) => [o, fmt(n)])),
    '',
  ].join('\n');

  await Deno.writeTextFile(args.reportPath, report);
  console.log(`clean: ${fmt(rowsIn)} in → ${fmt(rowsOut)} out (${fmt(rejectTotal)} rejected, ${fmt(duplicatesDropped)} duplicate spellings dropped; 14-digit rows out: ${fmt(fourteen)}) → ${args.outPath}; report → ${args.reportPath}`);
}

if (import.meta.main) {
  await main();
}
