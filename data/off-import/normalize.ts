/**
 * MODULE: scripts/data-import/off/normalize.ts
 * PURPOSE: The pure, testable core of the OFF cleaner — turns one extracted
 *          dump row into either a load row for `ref_products_off_staging`
 *          or a typed reject, using the APP'S OWN CODE for every decision:
 *            - barcode gate + canonical form: normalizeScannedBarcode
 *              (src/modules/barcode/barcodeClassifier.ts) — digits only,
 *              length ∈ {8, 12, 13, 14}, GS1 mod-10, and the 13-digit '0…'
 *              → 12-digit UPC-A fold. The dump spells EVERY UPC-A as
 *              13-digit '0…' (probe, 2026-09-15), so the fold is the join
 *              key between the dump and items.barcode.
 *            - name / brand / categories / size / organic: mapOffProduct
 *              (supabase/functions/off-proxy/handler.ts) — the SAME function
 *              the live proxy runs on a live OFF answer, fed the dump row
 *              shaped like an API product object. A local row is therefore
 *              byte-for-byte what the live path would have produced for that
 *              product: first brand of `brands`, en: tags preferred, the
 *              strict unit allowlist, multipacks skipped, serving-size echo
 *              suppressed, is_organic only on en:usda-organic.
 *            - category_id: mapOffCategoriesToCanonical
 *              (src/modules/barcode/offCategoryMapper.ts) → canonical name →
 *              the environment's item_categories id (ids are
 *              gen_random_uuid() per project, so the id map is an INPUT
 *              exported from the target DB, never guessed).
 * WHY PURE: no I/O here so the Deno tests can prove every rule on synthetic
 *          rows (bracketed placeholders, never real brand or product names).
 *
 * THREAT MODEL: the dump is community-edited, third-party text. Nothing in it
 *   is trusted: every field is typeof-checked, the barcode passes the same
 *   gate a scanned payload does, name/brand are length-capped to the DB's
 *   CHECKs (a row that would violate a CHECK is rejected here so a \copy
 *   never aborts half-way), and the CSV / array literals are escaped by the
 *   functions below — a product name containing a quote, a comma, a brace or
 *   a newline is data, never syntax.
 */

import {
  hasRestrictedCirculationPrefix,
  normalizeScannedBarcode,
} from '../../../src/modules/barcode/barcodeClassifier.ts';
import { mapOffCategoriesToCanonical } from '../../../src/modules/barcode/offCategoryMapper.ts';
import { mapOffProduct } from '../../../supabase/functions/off-proxy/handler.ts';

// ── Extract row (what extract.py writes) ──────────────────────────────────────

export interface ExtractRow {
  code: string;
  name_main: string | null;
  name_en: string | null;
  brands: string | null;
  categories: string | null;
  categories_tags: string[] | null;
  quantity: string | null;
  product_quantity: string | null;
  product_quantity_unit: string | null;
  serving_size: string | null;
  labels_tags: string[] | null;
  last_modified_t: number;
  is_us: boolean;
}

function optString(v: unknown): string | null {
  return typeof v === 'string' ? v : null;
}

function optStringList(v: unknown): string[] | null {
  if (!Array.isArray(v)) return null;
  return v.filter((x): x is string => typeof x === 'string');
}

/** typeof-checks one parsed JSONL object; null when it is not a usable row. */
export function parseExtractRow(value: unknown): ExtractRow | null {
  if (typeof value !== 'object' || value === null) return null;
  const o = value as Record<string, unknown>;
  if (typeof o.code !== 'string') return null;
  const lastModified =
    typeof o.last_modified_t === 'number' && Number.isFinite(o.last_modified_t)
      ? o.last_modified_t
      : null;
  if (lastModified === null) return null;
  return {
    code: o.code,
    name_main: optString(o.name_main),
    name_en: optString(o.name_en),
    brands: optString(o.brands),
    categories: optString(o.categories),
    categories_tags: optStringList(o.categories_tags),
    quantity: optString(o.quantity),
    product_quantity: optString(o.product_quantity),
    product_quantity_unit: optString(o.product_quantity_unit),
    serving_size: optString(o.serving_size),
    labels_tags: optStringList(o.labels_tags),
    last_modified_t: lastModified,
    is_us: o.is_us === true,
  };
}

// ── Load row (what \copy reads into ref_products_off_staging) ─────────────────

/** Column order of the load CSV = the \copy column list. loaded_at is left
 *  to its DEFAULT. */
export const LOAD_COLUMNS = [
  'barcode',
  'off_code',
  'name',
  'brand',
  'size_value',
  'size_unit',
  'category_id',
  'categories_tags',
  'is_organic',
  'labels_tags',
  'off_quantity',
  'off_last_modified',
  'origin',
] as const;

export interface LoadRow {
  barcode: string;
  off_code: string;
  name: string;
  brand: string | null;
  size_value: number | null;
  size_unit: string | null;
  category_id: string | null;
  /** The canonical name behind category_id (report only — not a column). */
  category_name: string | null;
  categories_tags: string[] | null;
  is_organic: true | null;
  labels_tags: string[] | null;
  off_quantity: string | null;
  off_last_modified: string;
  origin: 'dump';
}

export type RejectReason =
  | 'malformed_row'
  | 'bad_barcode'
  | 'restricted_circulation'
  | 'empty_name'
  | 'name_too_long';

export type CleanResult =
  | {
      kind: 'ok';
      row: LoadRow;
      /** The proxy mapped at least one category string from the tags. */
      hadCategories: boolean;
      /** A canonical category was found for those strings. */
      categoryMapped: boolean;
      /** The proxy parsed (and did not suppress) a size. */
      sizeParsed: boolean;
      /** The dump had a quantity label but no size came out (unparseable,
       *  multipack, unmappable unit, or the serving-size echo). */
      quantityUnparsed: boolean;
      /** brand was dropped for exceeding the DB's 500-char CHECK. */
      brandDropped: boolean;
    }
  | { kind: 'reject'; reason: RejectReason };

/** items_name_max_length / items_brand_max_length (20260530) and the
 *  ref_products_off CHECKs mirror them. */
const MAX_TEXT = 500;

/** The app's canonical form for a dump code, or null when the code fails
 *  the scanned-payload gate (length, digits, check digit). */
export function canonicalBarcode(code: string): string | null {
  return normalizeScannedBarcode(code);
}

/**
 * STANDING RULE (Eman, gate 3, 2026-09-16): a number-system-2 code — GS1
 * prefix 20–29, the restricted-circulation range stores use for
 * variable-weight and in-store items — NEVER enters a reference table, from
 * either source. Such a code is assigned by a shop, not registered
 * globally: the same 12 digits name different products in different
 * stores, so a reference row for it would be wrong for every store but
 * one. Decided by the app's own prefix rule (hasRestrictedCirculationPrefix,
 * the is_store_brand signal) on the CANONICAL form. Whether the scan gate
 * should also treat prefix-2 as non-identity is parked for phase 4.
 */
export function isRestrictedCirculation(barcode: string): boolean {
  return hasRestrictedCirculationPrefix(barcode);
}

/**
 * Gate + canonical form + the reference-table rule in one step: the
 * canonical barcode when the row may enter the table, or the reject reason.
 */
export function referenceBarcode(
  code: string,
): { barcode: string } | { reject: 'bad_barcode' | 'restricted_circulation' } {
  const barcode = canonicalBarcode(code);
  if (barcode === null) return { reject: 'bad_barcode' };
  if (isRestrictedCirculation(barcode)) return { reject: 'restricted_circulation' };
  return { barcode };
}

/** Epoch seconds → ISO-8601 UTC, the form Postgres reads into timestamptz. */
export function epochToIso(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString();
}

/**
 * One dump row → load row or reject. `categoryIds` maps the canonical
 * category NAME (exactly as in src/constants/categories.ts) to the target
 * environment's item_categories.id.
 */
export function cleanRow(
  raw: ExtractRow,
  categoryIds: ReadonlyMap<string, string>,
): CleanResult {
  const gate = referenceBarcode(raw.code);
  if ('reject' in gate) return { kind: 'reject', reason: gate.reject };
  const barcode = gate.barcode;

  // Shape the dump row like an OFF API product object and run the proxy's
  // own mapping — the single source of truth for every derived field.
  const productName = raw.name_main ?? raw.name_en;
  const product: Record<string, unknown> = {
    product_name: productName ?? undefined,
    brands: raw.brands ?? undefined,
    categories: raw.categories ?? undefined,
    categories_tags: raw.categories_tags ?? undefined,
    quantity: raw.quantity ?? undefined,
    product_quantity: raw.product_quantity ?? undefined,
    product_quantity_unit: raw.product_quantity_unit ?? undefined,
    serving_size: raw.serving_size ?? undefined,
    labels_tags: raw.labels_tags ?? undefined,
  };
  const mapped = mapOffProduct(product);

  if (mapped.name === undefined) return { kind: 'reject', reason: 'empty_name' };
  if (mapped.name.length > MAX_TEXT) return { kind: 'reject', reason: 'name_too_long' };

  let brand: string | null = mapped.brand ?? null;
  let brandDropped = false;
  if (brand !== null && brand.length > MAX_TEXT) {
    brand = null;
    brandDropped = true;
  }

  const hadCategories = mapped.categories !== undefined && mapped.categories.length > 0;
  const categoryName = hadCategories ? mapOffCategoriesToCanonical(mapped.categories) : null;
  const categoryId = categoryName !== null ? (categoryIds.get(categoryName) ?? null) : null;

  const sizeParsed = mapped.sizeValue !== undefined && mapped.sizeUnit !== undefined;
  const hadQuantity =
    (raw.quantity !== null && raw.quantity.trim() !== '') ||
    (raw.product_quantity !== null && raw.product_quantity.trim() !== '');

  return {
    kind: 'ok',
    row: {
      barcode,
      off_code: raw.code,
      name: mapped.name,
      brand,
      size_value: sizeParsed ? mapped.sizeValue! : null,
      size_unit: sizeParsed ? mapped.sizeUnit! : null,
      category_id: categoryId,
      category_name: categoryName,
      categories_tags: raw.categories_tags,
      is_organic: mapped.isOrganic === true ? true : null,
      labels_tags: raw.labels_tags,
      off_quantity: raw.quantity !== null && raw.quantity.trim() !== '' ? raw.quantity.trim() : null,
      off_last_modified: epochToIso(raw.last_modified_t),
      origin: 'dump',
    },
    hadCategories,
    categoryMapped: categoryName !== null,
    sizeParsed,
    quantityUnparsed: hadQuantity && !sizeParsed,
    brandDropped,
  };
}

// ── Dedupe (the dump has a handful of repeated codes; the PK must not) ───────

/**
 * Two-pass newest-wins selection keyed by canonical barcode. Pass 1
 * `observe`s every (barcode, last_modified_t); pass 2 asks `shouldEmit`,
 * which is true exactly once per barcode — for the first row carrying the
 * newest timestamp (ties: first seen wins, so output is deterministic for a
 * deterministic input order).
 */
export class NewestWins {
  private readonly newest = new Map<string, number>();
  private readonly emitted = new Set<string>();
  private duplicates = 0;

  observe(barcode: string, lastModified: number): void {
    const current = this.newest.get(barcode);
    if (current === undefined) {
      this.newest.set(barcode, lastModified);
    } else {
      this.duplicates += 1;
      if (lastModified > current) this.newest.set(barcode, lastModified);
    }
  }

  shouldEmit(barcode: string, lastModified: number): boolean {
    if (this.emitted.has(barcode)) return false;
    if (this.newest.get(barcode) !== lastModified) return false;
    this.emitted.add(barcode);
    return true;
  }

  /** Rows that were NOT the surviving copy of their barcode. */
  get duplicateCount(): number {
    return this.duplicates;
  }

  get keyCount(): number {
    return this.newest.size;
  }
}

// ── CSV / Postgres literal encoding ───────────────────────────────────────────

/**
 * One CSV field for `\copy … CSV`: NULL is an EMPTY UNQUOTED field (the
 * default NULL representation); any value containing a comma, a double
 * quote, CR or LF is quoted with embedded quotes doubled. An empty string
 * is quoted ("") so it is not read back as NULL.
 */
export function csvField(value: string | null): string {
  if (value === null) return '';
  if (value === '') return '""';
  if (/[",\r\n]/.test(value)) return '"' + value.replace(/"/g, '""') + '"';
  return value;
}

/**
 * A Postgres text[] literal: {"a","b"} with backslashes and double quotes
 * escaped inside each element. Every element is quoted so braces, commas
 * and whitespace inside a tag are data. An empty list is {}.
 */
export function pgArrayLiteral(values: readonly string[]): string {
  const parts = values.map((v) => '"' + v.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"');
  return '{' + parts.join(',') + '}';
}

/** The load CSV header line (the \copy column list, in order). */
export function csvHeader(): string {
  return LOAD_COLUMNS.join(',');
}

/** One load row as a CSV record (no trailing newline). */
export function toCsvRecord(row: LoadRow): string {
  const fields: Array<string | null> = [
    row.barcode,
    row.off_code,
    row.name,
    row.brand,
    row.size_value === null ? null : String(row.size_value),
    row.size_unit,
    row.category_id,
    row.categories_tags === null ? null : pgArrayLiteral(row.categories_tags),
    row.is_organic === true ? 't' : null,
    row.labels_tags === null ? null : pgArrayLiteral(row.labels_tags),
    row.off_quantity,
    row.off_last_modified,
    row.origin,
  ];
  return fields.map(csvField).join(',');
}

// ── The categories id map (an input, exported from the target DB) ────────────

/** Minimal RFC-4180 line parser — enough for psql's `\copy … CSV HEADER`
 *  output (quoted fields, doubled quotes; no embedded newlines in names). */
export function parseCsvLine(line: string): string[] {
  const out: string[] = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]!;
    if (quoted) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ',') {
      out.push(field);
      field = '';
    } else {
      field += ch;
    }
  }
  out.push(field);
  return out;
}

/**
 * Parses `\copy (select id, name from item_categories) to … csv header`
 * output into name → id. Header order is detected by name so either column
 * order works; a file without both columns throws — a wrong map is worse
 * than none.
 */
export function parseCategoriesCsv(text: string): Map<string, string> {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== '');
  if (lines.length === 0) throw new Error('categories CSV is empty');
  const header = parseCsvLine(lines[0]!).map((h) => h.trim().toLowerCase());
  const idIdx = header.indexOf('id');
  const nameIdx = header.indexOf('name');
  if (idIdx < 0 || nameIdx < 0) {
    throw new Error(`categories CSV needs "id" and "name" columns; got: ${header.join(',')}`);
  }
  const map = new Map<string, string>();
  for (const line of lines.slice(1)) {
    const cols = parseCsvLine(line);
    const id = cols[idIdx]?.trim();
    const name = cols[nameIdx]?.trim();
    if (id && name) map.set(name, id);
  }
  return map;
}
