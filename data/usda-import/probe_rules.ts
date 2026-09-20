/**
 * MODULE: scripts/data-import/usda/probe_rules.ts — the APP-RULES half of the
 *         USDA GBFPD probe (data-source arc phase 3 §1a, 2026-09-19)
 * PURPOSE: probe.py (DuckDB) joins and dedupes the USDA CSV release and
 *          writes one small JSONL row per barcode; THIS script runs the
 *          app's OWN code over every row and writes the verdicts back as
 *          JSONL for probe.py to aggregate:
 *            - the barcode gate + canonical form: normalizeScannedBarcode
 *              (src/modules/barcode/barcodeClassifier.ts) after the ONE
 *              pre-fold the gate does not do itself — a 14-digit GTIN whose
 *              packaging indicator is '0' is reduced to its 13-digit body
 *              (the gate then folds a '0…' body to the 12-digit UPC-A); a
 *              14-digit code with a NON-ZERO indicator is a case / logistics
 *              code, never a retail identity, and is reported separately;
 *            - the prefix-2 rule: hasRestrictedCirculationPrefix on the
 *              canonical form (standing decision #11);
 *            - the size grammar: the proxy's mapOffProduct (the same
 *              parseOffSize the OFF cleaner used) on the raw package_weight
 *              AND on its FIRST slash-separated token ("16 oz/454 g" →
 *              "16 oz"), so the report can show both rates;
 *            - the store-brand list: classifyStoreBrand on
 *              (description, brand_name || brand_owner) and on brand_owner
 *              alone (the app's KNOWN_STORE_BRANDS token match);
 *            - the category mapper: mapOffCategoriesToCanonical on the USDA
 *              category string (name similarity as the app defines it).
 * WHY A SEPARATE DENO STEP: standing decision #7 — every normalisation
 *          decision is the app's own code, never a Python re-port. DuckDB
 *          does only what SQL is good at (join, dedupe, projection,
 *          aggregation); Deno runs the TypeScript unmodified through
 *          scripts/data-import/deno.json (import map + sloppy imports).
 * REPORT-ONLY: reads one local file, writes one local file, no network,
 *          no Supabase. The §3 cleaner (clean.ts / normalize.ts) supersedes
 *          these rules with tested, exported functions; this file is the
 *          measurement that informs the §1c picks and is not imported by
 *          anything.
 *
 * RUN (from the repo root; probe.py runs it for you):
 *   deno run --config scripts/data-import/deno.json \
 *     --allow-read=scripts/data-import/usda/out --allow-write=scripts/data-import/usda/out \
 *     scripts/data-import/usda/probe_rules.ts --in <dedup.jsonl> --out <rules.jsonl>
 */

import { readJsonLines } from '../off/jsonl.ts';
import {
  classifyStoreBrand,
  hasRestrictedCirculationPrefix,
  normalizeScannedBarcode,
} from '../../../src/modules/barcode/barcodeClassifier.ts';
import { mapOffCategoriesToCanonical } from '../../../src/modules/barcode/offCategoryMapper.ts';
import { mapOffProduct } from '../../../supabase/functions/off-proxy/handler.ts';

/** What probe.py writes per deduped barcode (short keys — 465K lines). */
interface InRow {
  /** gtin_upc, trimmed */
  g: string;
  /** package_weight ('' when empty) */
  w: string;
  /** brand_name ('' when empty) */
  b: string;
  /** brand_owner ('' when empty) */
  o: string;
  /** food.csv description ('' when empty) */
  d: string;
  /** branded_food_category ('' when empty) */
  c: string;
}

/** What this script writes back per barcode. */
interface OutRow {
  g: string;
  /** The app's canonical form, or null when the code cannot enter a reference table by shape. */
  canon: string | null;
  /** ok | case_code (14-digit, non-zero indicator) | gate (normalizeScannedBarcode returned null) */
  reason: 'ok' | 'case_code' | 'gate';
  /** hasRestrictedCirculationPrefix(canon) — standing decision #11; false when canon is null. */
  restricted: boolean;
  /** Shape bucket of the raw package_weight (see shapeOf). */
  shape: string;
  /** Size from the FULL package_weight string through the proxy's grammar (null = no parse). */
  fv: number | null;
  fu: string | null;
  /** Size from the FIRST slash-separated token through the same grammar. */
  tv: number | null;
  tu: string | null;
  /** classifyStoreBrand(description, brand_name || brand_owner) — the app's rule as it stands. */
  sb: boolean;
  /** classifyStoreBrand(null, brand_owner) — does the app's list catch the owner string alone? */
  sbo: boolean;
  /** mapOffCategoriesToCanonical([branded_food_category]) — the app's name-similarity rule. */
  cm: string | null;
}

const SINGLE_TOKEN = /^[0-9]+(?:[.,][0-9]+)?\s*[a-zA-Z][a-zA-Z. ]*$/;
const MULTIPACK = /[0-9]\s*[x×*]\s*[0-9]/i;

/** A coarse, human-readable shape bucket for the raw package_weight string. */
function shapeOf(raw: string): string {
  const w = raw.trim();
  if (w === '') return 'empty';
  if (MULTIPACK.test(w)) return 'multipack (n x n …)';
  const segs = w.split('/').map((s) => s.trim());
  if (segs.length === 1) {
    if (SINGLE_TOKEN.test(w)) return 'single "<n> <unit>"';
    if (w.includes('(')) return 'parenthesised';
    return 'other, no slash';
  }
  if (segs.length === 2) {
    return segs.every((s) => SINGLE_TOKEN.test(s)) ? 'dual "<n> <unit>/<n> <unit>"' : 'two segments, other';
  }
  return 'three or more slash segments';
}

/**
 * The cleaner-side pre-fold the app's gate does not perform (see the module
 * header): a 14-digit GTIN with indicator '0' → its 13-digit body; a
 * non-zero indicator → case code (excluded). Everything else is passed to
 * the gate untouched — normalizeScannedBarcode strips whitespace, gates on
 * digits / length / check digit and folds a 13-digit '0…' to 12.
 */
function canonicalOf(raw: string): { canon: string | null; reason: OutRow['reason'] } {
  const g = raw.trim();
  let candidate = g;
  if (/^[0-9]{14}$/.test(g)) {
    if (g[0] !== '0') return { canon: null, reason: 'case_code' };
    candidate = g.slice(1);
  }
  const canon = normalizeScannedBarcode(candidate);
  return canon === null ? { canon: null, reason: 'gate' } : { canon, reason: 'ok' };
}

function sizeOf(quantity: string): { v: number | null; u: string | null } {
  if (quantity.trim() === '') return { v: null, u: null };
  const mapped = mapOffProduct({ quantity });
  return mapped.sizeValue !== undefined && mapped.sizeUnit !== undefined
    ? { v: mapped.sizeValue, u: mapped.sizeUnit }
    : { v: null, u: null };
}

function parseArgs(argv: readonly string[]): { inPath: string; outPath: string } {
  const get = (flag: string): string => {
    const i = argv.indexOf(flag);
    const v = i >= 0 ? argv[i + 1] : undefined;
    if (v === undefined) throw new Error(`missing ${flag}`);
    return v;
  };
  return { inPath: get('--in'), outPath: get('--out') };
}

async function main(): Promise<void> {
  const { inPath, outPath } = parseArgs(Deno.args);
  const encoder = new TextEncoder();
  const out = await Deno.open(outPath, { write: true, create: true, truncate: true });
  let chunk: string[] = [];
  let rows = 0;
  const flush = async () => {
    if (chunk.length === 0) return;
    const bytes = encoder.encode(chunk.join(''));
    let written = 0;
    while (written < bytes.length) written += await out.write(bytes.subarray(written));
    chunk = [];
  };
  for await (const value of readJsonLines(inPath)) {
    if (typeof value !== 'object' || value === null) continue;
    const r = value as Partial<InRow>;
    if (typeof r.g !== 'string') continue;
    const w = typeof r.w === 'string' ? r.w : '';
    const b = typeof r.b === 'string' ? r.b : '';
    const o = typeof r.o === 'string' ? r.o : '';
    const d = typeof r.d === 'string' ? r.d : '';
    const c = typeof r.c === 'string' ? r.c : '';

    const { canon, reason } = canonicalOf(r.g);
    const full = sizeOf(w);
    const first = sizeOf(w.split('/')[0] ?? '');
    const brand = b.trim() !== '' ? b : o;
    const row: OutRow = {
      g: r.g,
      canon,
      reason,
      restricted: canon !== null && hasRestrictedCirculationPrefix(canon),
      shape: shapeOf(w),
      fv: full.v,
      fu: full.u,
      tv: first.v,
      tu: first.u,
      sb: classifyStoreBrand(d.trim() !== '' ? d : null, brand.trim() !== '' ? brand : null),
      sbo: classifyStoreBrand(null, o.trim() !== '' ? o : null),
      cm: c.trim() !== '' ? mapOffCategoriesToCanonical([c]) : null,
    };
    chunk.push(JSON.stringify(row), '\n');
    rows += 1;
    if (chunk.length >= 20_000) await flush();
  }
  await flush();
  out.close();
  console.log(`probe_rules: ${rows.toLocaleString('en-US')} rows → ${outPath}`);
}

if (import.meta.main) {
  await main();
}
