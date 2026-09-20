/**
 * MODULE: scripts/data-import/usda/normalize.ts
 * PURPOSE: The pure, testable core of the USDA cleaner — turns one extracted
 *          release row into either a load row for `ref_products_usda_staging`
 *          or a typed reject, using the APP'S OWN CODE for every decision
 *          (phase-3 gate-1 picks, 2026-09-20):
 *            - barcode: the ONE pre-fold the app's gate does not perform (a
 *              14-digit GTIN with packaging indicator '0' → its 13-digit
 *              body; a NON-ZERO indicator is a case / logistics code →
 *              reject `case_code`), then normalizeScannedBarcode
 *              (src/modules/barcode/barcodeClassifier.ts — digits, length
 *              ∈ {8, 12, 13, 14}, GS1 mod-10, the 13-digit '0…' → 12 fold)
 *              and hasRestrictedCirculationPrefix (standing decision #11:
 *              a number-system-2 code never enters a reference table);
 *            - brand: brand_name unless empty or a placeholder literal
 *              ('N/A', 'NA'), else brand_owner;
 *            - name: food.csv `description`, title-cased ONLY when the source
 *              has no lowercase letter (97.3 % of rows) — the single casing
 *              rule; nothing else is polished;
 *            - size: the proxy's mapOffProduct (supabase/functions/off-proxy/
 *              handler.ts — the same size grammar the OFF cleaner runs) fed
 *              the FIRST slash-separated token of package_weight ("16 oz/454 g"
 *              → "16 oz"; the release writes dual-unit labels on 68 % of rows);
 *            - category: usda_category_map.csv (COMMITTED; Eman-reviewed) →
 *              canonical name → the target environment's item_categories id
 *              (ids are gen_random_uuid() per project, so the id map is an
 *              INPUT exported from the target DB, never guessed). ANY
 *              non-empty canonical counts as mapped, whatever the row's
 *              status string says ('auto' / 'user map');
 *            - is_store_brand: the app's classifyStoreBrand(name, brand) OR
 *              brand_owner containing an entry of usda_retailer_owners.csv
 *              (COMMITTED; retailers only — no wholesaler co-ops, the
 *              knownStoreBrands.ts rule).
 * WHY PURE: no I/O here so the Deno tests can prove every rule on synthetic
 *          rows (bracketed placeholders, never real brand or product names).
 * SHARED WITH THE OFF CLEANER: the CSV / array encoders, the categories-map
 *          parser and the two-pass NewestWins dedupe are imported from
 *          ../off/normalize.ts — one implementation, two cleaners (the
 *          `\copy … CSV HEADER` dialect is the same for both tables).
 *
 * THREAT MODEL: the release is third-party text (manufacturer-submitted).
 *   Nothing in it is trusted: every field is typeof-checked, the barcode
 *   passes the same gate a scanned payload does, name / brand / owner are
 *   length-capped to the DB's CHECKs (a row that would violate a CHECK is
 *   rejected here so a \copy never aborts half-way), and the CSV fields are
 *   escaped by the shared encoder — a description containing a quote, a
 *   comma or a newline is data, never syntax. THE 14-DIGIT GAP: the DB's
 *   barcode CHECK accepts any 14-digit string, so nothing downstream would
 *   catch a `00…` row that skipped the pre-fold — the tests here and the
 *   load read-back (`length(barcode) = 14` → 0) are the enforcement.
 */

import {
  classifyStoreBrand,
  hasRestrictedCirculationPrefix,
  normalizeScannedBarcode,
} from '../../../src/modules/barcode/barcodeClassifier.ts';
import { mapOffProduct } from '../../../supabase/functions/off-proxy/handler.ts';
import { csvField, parseCsvLine } from '../off/normalize.ts';

// ── Extract row (what extract.py writes) ──────────────────────────────────────

export interface ExtractRow {
  gtin: string;
  fdc_id: number;
  brand_owner: string | null;
  brand_name: string | null;
  category: string | null;
  package_weight: string | null;
  description: string | null;
  /** YYYY-MM-DD (food.csv publication_date) */
  publication_date: string;
  market_country: string | null;
}

function optString(v: unknown): string | null {
  return typeof v === 'string' ? v : null;
}

const ISO_DATE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

/** typeof-checks one parsed JSONL object; null when it is not a usable row. */
export function parseExtractRow(value: unknown): ExtractRow | null {
  if (typeof value !== 'object' || value === null) return null;
  const o = value as Record<string, unknown>;
  if (typeof o.gtin !== 'string') return null;
  if (typeof o.fdc_id !== 'number' || !Number.isInteger(o.fdc_id) || o.fdc_id <= 0) return null;
  if (typeof o.publication_date !== 'string' || !ISO_DATE.test(o.publication_date)) return null;
  return {
    gtin: o.gtin,
    fdc_id: o.fdc_id,
    brand_owner: optString(o.brand_owner),
    brand_name: optString(o.brand_name),
    category: optString(o.category),
    package_weight: optString(o.package_weight),
    description: optString(o.description),
    publication_date: o.publication_date,
    market_country: optString(o.market_country),
  };
}

/** YYYY-MM-DD → epoch seconds (the NewestWins key; a bad date is 0). */
export function dateToEpoch(isoDate: string): number {
  const t = Date.parse(isoDate + 'T00:00:00Z');
  return Number.isFinite(t) ? Math.floor(t / 1000) : 0;
}

// ── Load row (what \copy reads into ref_products_usda_staging) ────────────────

/** Column order of the load CSV = the \copy column list. loaded_at is left
 *  to its DEFAULT. */
export const LOAD_COLUMNS = [
  'barcode',
  'gtin_upc_raw',
  'name',
  'brand',
  'brand_owner',
  'size_value',
  'size_unit',
  'category_id',
  'usda_category',
  'package_weight_raw',
  'fdc_id',
  'usda_publication_date',
  'is_store_brand',
] as const;

export interface LoadRow {
  barcode: string;
  gtin_upc_raw: string;
  name: string;
  brand: string | null;
  brand_owner: string | null;
  size_value: number | null;
  size_unit: string | null;
  category_id: string | null;
  /** The canonical name behind category_id (report only — not a column). */
  category_name: string | null;
  usda_category: string | null;
  package_weight_raw: string | null;
  fdc_id: number;
  usda_publication_date: string;
  is_store_brand: boolean;
}

export type RejectReason =
  | 'malformed_row'
  | 'case_code'
  | 'bad_barcode'
  | 'restricted_circulation'
  | 'empty_name'
  | 'name_too_long';

export type CleanResult =
  | {
      kind: 'ok';
      row: LoadRow;
      /** brand came from brand_owner (brand_name empty or a placeholder). */
      brandFromOwner: boolean;
      /** brand_name was a placeholder literal ('N/A' / 'NA'). */
      brandPlaceholder: boolean;
      /** The name was all-caps and got the title-case pass. */
      titleCased: boolean;
      /** A size parsed from the first package_weight token. */
      sizeParsed: boolean;
      /** package_weight was present but no size came out. */
      weightUnparsed: boolean;
      /** The category string is non-empty. */
      hadCategory: boolean;
      /** A canonical name was found in the map for it. */
      categoryMapped: boolean;
      /** is_store_brand came from the app's own classifier. */
      storeBrandByApp: boolean;
      /** is_store_brand came from the owner list (possibly as well). */
      storeBrandByOwner: boolean;
      /** brand / brand_owner dropped for exceeding the DB's 500-char CHECK. */
      brandDropped: boolean;
      /** The raw spelling had 14 digits with indicator 0 (pre-folded). */
      preFolded: boolean;
      /** The raw spelling carried whitespace that was stripped. */
      whitespaceStripped: boolean;
    }
  | { kind: 'reject'; reason: RejectReason };

/** The ref_products_usda CHECKs mirror items' 500-char name / brand caps. */
const MAX_TEXT = 500;
const MAX_CATEGORY = 200;

// ── Barcode ───────────────────────────────────────────────────────────────────

/**
 * The release occasionally spells a code with an embedded space ("0 4338107"
 * — found by the 2026-09-20 full-row \copy proof: the app's gate strips
 * whitespace, so the canonical form passed while the raw spelling failed
 * the DB's digits CHECK). ALL whitespace is stripped before the pre-fold,
 * exactly as normalizeScannedBarcode does, and the stripped spelling is
 * what gtin_upc_raw stores.
 */
export function stripWhitespace(raw: string): string {
  return raw.replace(/\s+/g, '');
}

/**
 * The cleaner-side PRE-FOLD (phase-3 §1b, executed 2026-09-19): the app's
 * gate never folds a 14-digit code, so a GTIN-14 whose packaging indicator
 * is '0' is reduced to its 13-digit body here and handed to the gate (which
 * folds a '0…' body to the 12-digit UPC-A and keeps a non-zero 13). A
 * non-zero indicator is a case / logistics code — never a retail identity.
 */
export function preFoldGtin14(raw: string): { candidate: string; preFolded: boolean } | { reject: 'case_code' } {
  const g = stripWhitespace(raw);
  if (/^[0-9]{14}$/.test(g)) {
    if (g[0] !== '0') return { reject: 'case_code' };
    return { candidate: g.slice(1), preFolded: true };
  }
  return { candidate: g, preFolded: false };
}

/**
 * Pre-fold + gate + canonical form + the reference-table rule (standing #11)
 * in one step: the canonical barcode when the row may enter the table, or
 * the reject reason.
 */
export function referenceBarcode(
  raw: string,
): { barcode: string; preFolded: boolean } | { reject: 'case_code' | 'bad_barcode' | 'restricted_circulation' } {
  const pre = preFoldGtin14(raw);
  if ('reject' in pre) return { reject: pre.reject };
  const barcode = normalizeScannedBarcode(pre.candidate);
  if (barcode === null) return { reject: 'bad_barcode' };
  if (hasRestrictedCirculationPrefix(barcode)) return { reject: 'restricted_circulation' };
  return { barcode, preFolded: pre.preFolded };
}

// ── Brand ─────────────────────────────────────────────────────────────────────

/** Placeholder literals the release writes where a brand is unknown. */
const BRAND_PLACEHOLDERS = new Set(['N/A', 'NA', 'N/A.', 'NONE', 'NOT APPLICABLE']);

export function isBrandPlaceholder(value: string): boolean {
  return BRAND_PLACEHOLDERS.has(value.trim().toUpperCase());
}

function cleanText(value: string | null): string | null {
  if (value === null) return null;
  const t = value.replace(/\s+/g, ' ').trim();
  return t === '' ? null : t;
}

/**
 * brand_name unless empty or a placeholder, else brand_owner (the gate-1
 * rule). Returns the brand and which source it came from.
 */
export function pickBrand(
  brandName: string | null,
  brandOwner: string | null,
): { brand: string | null; fromOwner: boolean; placeholder: boolean } {
  const name = cleanText(brandName);
  const placeholder = name !== null && isBrandPlaceholder(name);
  if (name !== null && !placeholder) return { brand: name, fromOwner: false, placeholder: false };
  const owner = cleanText(brandOwner);
  return { brand: owner !== null && !isBrandPlaceholder(owner) ? owner : null, fromOwner: owner !== null, placeholder };
}

// ── Name casing ───────────────────────────────────────────────────────────────

/** True when the text has letters and none of them is lowercase. */
export function isAllCaps(text: string): boolean {
  return /[A-Za-z]/.test(text) && text === text.toUpperCase();
}

/**
 * THE casing rule (the only polish, gate-1 2026-09-20): lowercase the whole
 * string, then capitalise the first letter of every word. A word starts at
 * the beginning or after any character that is not a letter, a digit or an
 * apostrophe — so "HAM & CHEESE" → "Ham & Cheese", "LOW-FAT" → "Low-Fat",
 * "CALLENDER'S" → "Callender's", "SALT 'N PEPPER" → "Salt 'n Pepper".
 * Acronyms are lost by design ("BBQ" → "Bbq") — cheaper than a dictionary,
 * and a user confirms the name anyway. Mixed-case input is returned as-is.
 */
export function titleCaseIfAllCaps(text: string): { name: string; titleCased: boolean } {
  if (!isAllCaps(text)) return { name: text, titleCased: false };
  const lower = text.toLowerCase();
  let out = '';
  let atWordStart = true;
  for (const ch of lower) {
    if (/[a-z]/.test(ch)) {
      out += atWordStart ? ch.toUpperCase() : ch;
      atWordStart = false;
    } else if (/[0-9'’]/.test(ch)) {
      out += ch;
      atWordStart = false;
    } else {
      out += ch;
      atWordStart = true;
    }
  }
  return { name: out, titleCased: true };
}

// ── Size ──────────────────────────────────────────────────────────────────────

/**
 * The app's size grammar (the proxy's mapOffProduct → parseOffSize) on the
 * FIRST slash-separated token of the label. The release writes
 * "<imperial>/<metric>" on most rows; the OFF grammar is whole-string and
 * would parse none of them (phase-3 §1a: 9.5 % vs 97.4 %).
 */
export function parseSizeFirstToken(packageWeight: string | null): { value: number; unit: string } | null {
  if (packageWeight === null) return null;
  const first = (packageWeight.split('/')[0] ?? '').trim();
  if (first === '') return null;
  const mapped = mapOffProduct({ quantity: first });
  return mapped.sizeValue !== undefined && mapped.sizeUnit !== undefined
    ? { value: mapped.sizeValue, unit: mapped.sizeUnit }
    : null;
}

// ── Category map + owner list (COMMITTED inputs beside the scripts) ──────────

/**
 * Parses usda_category_map.csv (`usda_category,rows_kept,canonical,status,rule`)
 * into USDA category → canonical name. ANY non-empty canonical counts,
 * whatever the status column says ('auto', 'user map', …). A file without
 * both columns throws — a wrong map is worse than none.
 */
export function parseCategoryMapCsv(text: string): Map<string, string> {
  const lines = text.replace(/^﻿/, '').split(/\r?\n/).filter((l) => l.trim() !== '');
  if (lines.length === 0) throw new Error('category map CSV is empty');
  const header = parseCsvLine(lines[0]!).map((h) => h.trim().toLowerCase());
  const catIdx = header.indexOf('usda_category');
  const canonIdx = header.indexOf('canonical');
  if (catIdx < 0 || canonIdx < 0) {
    throw new Error(`category map CSV needs "usda_category" and "canonical" columns; got: ${header.join(',')}`);
  }
  const map = new Map<string, string>();
  for (const line of lines.slice(1)) {
    const cols = parseCsvLine(line);
    // Keys are whitespace-collapsed exactly as cleanRow collapses the
    // category before the lookup — the release writes double spaces in
    // some category names ("… Beverages  Ready to Drink").
    const cat = cols[catIdx]?.replace(/\s+/g, ' ').trim();
    const canon = cols[canonIdx]?.trim();
    if (cat && canon) map.set(cat, canon);
  }
  return map;
}

/**
 * The categories the map lists WITHOUT a canonical — a recorded decision
 * ("stays blank", e.g. non-food), not a gap. The report keeps them apart
 * from categories the map has never seen (the real "unmapped" signal —
 * a new vocabulary in the release).
 */
export function parseCategoryMapBlanks(text: string): Set<string> {
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).filter((l) => l.trim() !== '');
  if (lines.length === 0) return new Set();
  const header = parseCsvLine(lines[0]!).map((h) => h.trim().toLowerCase());
  const catIdx = header.indexOf('usda_category');
  const canonIdx = header.indexOf('canonical');
  if (catIdx < 0 || canonIdx < 0) return new Set();
  const blanks = new Set<string>();
  for (const line of lines.slice(1)) {
    const cols = parseCsvLine(line);
    const cat = cols[catIdx]?.replace(/\s+/g, ' ').trim();
    const canon = cols[canonIdx]?.trim();
    if (cat && !canon) blanks.add(cat);
  }
  return blanks;
}

/**
 * Parses usda_retailer_owners.csv — one column `owner`, one retailer string
 * per row (comments start with #). Matching is case-insensitive substring
 * on brand_owner.
 */
export function parseOwnerListCsv(text: string): string[] {
  const lines = text.replace(/^﻿/, '').split(/\r?\n/).map((l) => l.trim()).filter((l) => l !== '' && !l.startsWith('#'));
  if (lines.length === 0) throw new Error('owner list CSV is empty');
  const header = parseCsvLine(lines[0]!).map((h) => h.trim().toLowerCase());
  if (header[0] !== 'owner') throw new Error(`owner list CSV needs an "owner" column; got: ${header.join(',')}`);
  return lines.slice(1).map((l) => parseCsvLine(l)[0]?.trim().toLowerCase() ?? '').filter((o) => o !== '');
}

export function ownerMatches(brandOwner: string | null, owners: readonly string[]): boolean {
  if (brandOwner === null) return false;
  const hay = brandOwner.toLowerCase();
  return owners.some((o) => hay.includes(o));
}

// ── The row ───────────────────────────────────────────────────────────────────

export interface CleanInputs {
  /** USDA category → canonical name (usda_category_map.csv). */
  categoryMap: ReadonlyMap<string, string>;
  /** canonical name → the target environment's item_categories.id. */
  categoryIds: ReadonlyMap<string, string>;
  /** lowercase retailer strings (usda_retailer_owners.csv). */
  owners: readonly string[];
}

/** One release row → load row or reject. */
export function cleanRow(raw: ExtractRow, inputs: CleanInputs): CleanResult {
  const gate = referenceBarcode(raw.gtin);
  if ('reject' in gate) return { kind: 'reject', reason: gate.reject };

  const description = cleanText(raw.description);
  if (description === null) return { kind: 'reject', reason: 'empty_name' };
  const cased = titleCaseIfAllCaps(description);
  if (cased.name.length > MAX_TEXT) return { kind: 'reject', reason: 'name_too_long' };

  const picked = pickBrand(raw.brand_name, raw.brand_owner);
  let brand = picked.brand;
  let brandOwner = cleanText(raw.brand_owner);
  if (brandOwner !== null && isBrandPlaceholder(brandOwner)) brandOwner = null;
  let brandDropped = false;
  if (brand !== null && brand.length > MAX_TEXT) {
    brand = null;
    brandDropped = true;
  }
  if (brandOwner !== null && brandOwner.length > MAX_TEXT) {
    brandOwner = null;
    brandDropped = true;
  }

  const size = parseSizeFirstToken(raw.package_weight);
  const weight = cleanText(raw.package_weight);

  const category = cleanText(raw.category);
  const usdaCategory = category !== null && category.length <= MAX_CATEGORY ? category : null;
  const categoryName = usdaCategory !== null ? (inputs.categoryMap.get(usdaCategory) ?? null) : null;
  const categoryId = categoryName !== null ? (inputs.categoryIds.get(categoryName) ?? null) : null;

  const byApp = classifyStoreBrand(cased.name, brand);
  const byOwner = ownerMatches(brandOwner, inputs.owners);

  return {
    kind: 'ok',
    row: {
      barcode: gate.barcode,
      gtin_upc_raw: stripWhitespace(raw.gtin),
      name: cased.name,
      brand,
      brand_owner: brandOwner,
      size_value: size?.value ?? null,
      size_unit: size?.unit ?? null,
      category_id: categoryId,
      category_name: categoryName,
      usda_category: usdaCategory,
      package_weight_raw: weight,
      fdc_id: raw.fdc_id,
      usda_publication_date: raw.publication_date,
      is_store_brand: byApp || byOwner,
    },
    brandFromOwner: picked.fromOwner,
    brandPlaceholder: picked.placeholder,
    titleCased: cased.titleCased,
    sizeParsed: size !== null,
    weightUnparsed: weight !== null && size === null,
    hadCategory: usdaCategory !== null,
    categoryMapped: categoryName !== null,
    storeBrandByApp: byApp,
    storeBrandByOwner: byOwner,
    brandDropped,
    preFolded: gate.preFolded,
    whitespaceStripped: stripWhitespace(raw.gtin) !== raw.gtin,
  };
}

// ── CSV encoding (the \copy dialect, shared encoder) ──────────────────────────

/** The load CSV header line (the \copy column list, in order). */
export function csvHeader(): string {
  return LOAD_COLUMNS.join(',');
}

/** One load row as a CSV record (no trailing newline). */
export function toCsvRecord(row: LoadRow): string {
  const fields: Array<string | null> = [
    row.barcode,
    row.gtin_upc_raw,
    row.name,
    row.brand,
    row.brand_owner,
    row.size_value === null ? null : String(row.size_value),
    row.size_unit,
    row.category_id,
    row.usda_category,
    row.package_weight_raw,
    String(row.fdc_id),
    row.usda_publication_date,
    row.is_store_brand ? 't' : 'f',
  ];
  return fields.map(csvField).join(',');
}
