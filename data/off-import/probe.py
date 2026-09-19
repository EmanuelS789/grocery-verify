#!/usr/bin/env python3
"""
scripts/data-import/off/probe.py

PURPOSE: Throwaway, READ-ONLY schema and shape probe of the Open Food Facts
         bulk export (`food.parquet`, the daily Parquet on Hugging Face —
         licence ODbL). Written for the DATA-SOURCE ARC phase 2 §1a
         diagnosis (PROGRESS.md 🧾 2026-09-15 entry). Prints a Markdown
         report to stdout; never writes to the repo, never touches the
         network or Supabase.

WHY DUCKDB, WHY PYTHON: the file is ~7.9 GB / ~4.7 M rows with nested
         STRUCT/LIST columns (product_name is STRUCT(lang, text)[] — there
         are NO language-suffixed columns in this export). DuckDB reads the
         Parquet footer for the schema, pushes column projection down so the
         nutrition / ingredients / images columns are never decoded, and
         streams the rest. The TypeScript cleaner (§3) consumes the CSV a
         later extraction step produces; this probe only measures.

WHY THE MOD-10 IS RE-IMPLEMENTED HERE: the app's gate is
         src/modules/barcode/barcodeClassifier.ts `hasValidGs1CheckDigit`
         (weights 3,1,3,1,… walking left from the digit beside the check
         digit). The TS module cannot be imported from Python, so the SAME
         algorithm is written twice below — once in SQL (fast, runs over
         every row) and once in Python — and the script REFUSES to run
         unless the two agree on a fixed self-test set (the usda-probe
         precedent, tightened).

INPUTS (all outside the repo — data never enters git):
  argv[1]            path to food.parquet
  --barcodes <file>  optional: one barcode per line (e.g. the USDA probe's
                     prod_barcodes.txt) → hit rates against the dump
  --limit N          optional: probe only the first N rows (smoke run)

RUN:
  pip install duckdb            (outside the repo; not an npm dependency)
  python scripts/data-import/off/probe.py <~/Documents/OFFData/food.parquet> \
      --barcodes <folder>/prod_barcodes.txt > report.md

NOT a test, NOT a build step, NOT in `npm run test:all`. No src/ or
supabase/ import in either direction.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

# Windows console default is cp1252; the report carries UTF-8 product text
# and arrows (the usda-probe crash, 2026-09-08).
sys.stdout.reconfigure(encoding="utf-8")

US_TAG = "en:united-states"
US_TAG_SHORT = "en:us"
SAMPLE_SEED = "20260915"

# The columns a cleaner would plausibly keep (standing decision #8: no
# nutrition, no ingredients, no images). Used ONLY for the trimmed-size
# estimate — the real kept set is a §1c option pick.
TRIM_COLUMNS = [
    "code",
    "product_name",  # the 'main' text only
    "brands",
    "brands_tags",
    "quantity",
    "product_quantity",
    "product_quantity_unit",
    "serving_size",
    "categories_tags",
    "labels_tags",
    "last_modified_t",
    "created_t",
]


# ── GS1 mod-10, twice ─────────────────────────────────────────────────────────

def gs1_check_ok_py(digits: str) -> bool:
    """Python port of barcodeClassifier.ts hasValidGs1CheckDigit."""
    if not digits.isdigit() or len(digits) < 2:
        return False
    total = 0
    n = len(digits)
    for i in range(n - 1):
        weight = 3 if (n - 1 - i) % 2 == 1 else 1
        total += int(digits[i]) * weight
    return (10 - (total % 10)) % 10 == int(digits[-1])


# SQL twin: i is 1-based, so (len - i) plays the role of (len-1-i) above.
def sql_gs1_ok(col: str) -> str:
    # CASE (not AND) so the digit casts are never evaluated on a non-digit or
    # empty value — DuckDB does not short-circuit AND across a whole scan.
    return (
        f"(CASE WHEN {col} ~ '^[0-9]{{2,}}$' THEN "
        f"(10 - (list_sum(list_transform(range(1, len({col})), "
        f"lambda i: TRY_CAST({col}[i] AS INTEGER) * (CASE WHEN (len({col}) - i) % 2 = 1 THEN 3 ELSE 1 END)"
        f")) % 10)) % 10 = TRY_CAST({col}[len({col})] AS INTEGER) ELSE false END)"
    )


SQL_GS1_OK = sql_gs1_ok("c")

SELF_TEST_CODES = [
    "013000626057",   # valid UPC-A (prod ketchup row)
    "0013000626057",  # same, 0-padded to 13 — still valid (leading-zero invariant)
    "058449410079",   # INVALID (§C4 digit-shifted guess)
    "523049410079",   # valid (§C4)
    "9780983263388",  # valid ISBN-13
    "4897041901047",  # valid EAN-13
    "12345678",       # EAN-8 shape, check digit wrong → invalid
    "96385074",       # valid EAN-8
    "00000000",       # valid (all zeros)
    "1234567890128",  # valid EAN-13
]


def self_test(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT c, " + SQL_GS1_OK + " AS ok FROM (SELECT unnest(?::VARCHAR[]) AS c)",
        [SELF_TEST_CODES],
    ).fetchall()
    for code, sql_ok in rows:
        py_ok = gs1_check_ok_py(code)
        if bool(sql_ok) != py_ok:
            raise SystemExit(f"mod-10 self-test FAILED on {code}: sql={sql_ok} py={py_ok}")


# ── Report helpers ────────────────────────────────────────────────────────────

def md_table(headers: list[str], rows: list[tuple]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v).replace("|", "\\|").replace("\n", " ") for v in r) + " |")
    return "\n".join(out)


def pct(n: int, d: int) -> str:
    return "n/a" if d == 0 else f"{100.0 * n / d:.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet")
    ap.add_argument("--barcodes", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args()

    path = args.parquet.replace("\\", "/")
    if not os.path.isfile(path):
        raise SystemExit(f"not a file: {path}")

    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    if args.threads:
        con.execute(f"SET threads = {args.threads}")
    self_test(con)

    src = f"read_parquet('{path}')"
    if args.limit:
        src = f"(SELECT * FROM {src} LIMIT {args.limit})"

    print(f"# OFF Parquet probe — `{os.path.basename(path)}`\n")
    print(f"- file size: {os.path.getsize(path):,} bytes")
    if args.limit:
        print(f"- ⚠️ SMOKE RUN: only the first {args.limit:,} rows were read")
    print(f"- DuckDB {duckdb.__version__}; mod-10 SQL/Python self-test: PASS on {len(SELF_TEST_CODES)} codes\n")

    # ── 1. schema ───────────────────────────────────────────────────────────
    schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    meta = con.execute(
        f"SELECT num_rows, num_row_groups, created_by FROM parquet_file_metadata('{path}')"
    ).fetchone()
    print("## 1. Schema (from the Parquet footer)\n")
    print(f"- rows (metadata): **{meta[0]:,}**; row groups: {meta[1]:,}; writer: `{meta[2]}`")
    print(f"- columns: **{len(schema)}**\n")
    print(md_table(["column", "type"], [(c, f"`{t}`") for c, t, *_ in schema]))
    col_names = {c for c, *_ in schema}
    print()
    print("Columns the §1a prompt named that do NOT exist as such: "
          + ", ".join(f"`{c}`" for c in ["obsolete_since_date", "product_name_en", "image_url", "image_small_url"] if c not in col_names)
          + ". `product_name` / `generic_name` are `STRUCT(lang, text)[]` lists (language keys inside the row, no suffixed columns); the only image columns are `images` (a struct list), `last_image_t`, `max_imgid`.\n")

    # ── 2. country filter (one aggregate pass) ─────────────────────────────
    t = time.time()
    agg = con.execute(f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE list_contains(countries_tags, '{US_TAG}')) AS us,
               count(*) FILTER (WHERE list_contains(countries_tags, '{US_TAG_SHORT}')) AS us_short,
               count(*) FILTER (WHERE list_contains(countries_tags, '{US_TAG}') AND list_contains(countries_tags, '{US_TAG_SHORT}')) AS both_tags,
               count(*) FILTER (WHERE list_contains(main_countries_tags, '{US_TAG}')) AS main_us,
               count(*) FILTER (WHERE obsolete) AS obsolete_all,
               count(*) FILTER (WHERE obsolete AND list_contains(countries_tags, '{US_TAG}')) AS obsolete_us,
               count(*) FILTER (WHERE countries_tags IS NULL OR len(countries_tags) = 0) AS no_country
        FROM {src}
    """).fetchone()
    total, us, us_short, both_tags, main_us, obs_all, obs_us, no_country = agg
    print("## 2. Country filter\n")
    print(md_table(["metric", "rows"], [
        ("total rows read", f"{total:,}"),
        (f"`countries_tags` contains `{US_TAG}`", f"**{us:,}** ({pct(us, total)})"),
        (f"`countries_tags` contains `{US_TAG_SHORT}`", f"{us_short:,}"),
        ("rows with BOTH tags", f"{both_tags:,}"),
        (f"`main_countries_tags` contains `{US_TAG}`", f"{main_us:,}"),
        ("`obsolete = true` (all rows)", f"{obs_all:,}"),
        ("`obsolete = true` AND US", f"{obs_us:,}"),
        ("rows with empty/NULL `countries_tags`", f"{no_country:,}"),
    ]))
    print(f"\n(`obsolete_since_date` does not exist in this export; `obsolete` is a BOOLEAN.) Pass time {time.time() - t:.1f}s\n")

    # ── 2b. what a countries_tags filter would MISS ────────────────────────
    # The app's identity form for a UPC-A is 12 digits; the dump spells it as
    # a 13-digit '0…' code. A UPC-A is a GS1-US/Canada number regardless of
    # how the community tagged the country, so count the NON-US-tagged rows
    # that look like a valid UPC-A with a name — the union a pure country
    # filter would leave behind.
    t = time.time()
    upc_a = (
        "len(code) = 13 AND code[1] = '0' AND code ~ '^[0-9]+$' AND "
        + sql_gs1_ok("code")
        + " AND list_filter(product_name, lambda x: x.lang = 'main')[1].text IS NOT NULL"
    )
    miss = con.execute(f"""
        SELECT count(*) FILTER (WHERE {upc_a} AND NOT coalesce(list_contains(countries_tags, '{US_TAG}'), false)) AS upc_a_non_us,
               count(*) FILTER (WHERE {upc_a} AND NOT coalesce(list_contains(countries_tags, '{US_TAG}'), false)
                                  AND (countries_tags IS NULL OR len(countries_tags) = 0)) AS upc_a_no_country,
               count(*) FILTER (WHERE {upc_a} AND list_contains(countries_tags, 'en:canada')
                                  AND NOT coalesce(list_contains(countries_tags, '{US_TAG}'), false)) AS upc_a_canada_only,
               count(*) FILTER (WHERE main_countries_tags IS NOT NULL AND len(main_countries_tags) > 0) AS main_countries_any,
               count(*) FILTER (WHERE obsolete IS NULL) AS obsolete_null
        FROM {src}
    """).fetchone()
    print("### 2b. What a pure `countries_tags` filter leaves behind (whole dump)\n")
    print(md_table(["metric", "rows"], [
        ("valid UPC-A-shaped code (13-digit `0…`, mod-10 OK) + a main name, NOT tagged US", f"**{miss[0]:,}**"),
        ("…of which with NO country tag at all", f"{miss[1]:,}"),
        ("…of which tagged `en:canada` (and not US)", f"{miss[2]:,}"),
        ("rows with any `main_countries_tags`", f"{miss[3]:,}"),
        ("rows with `obsolete` NULL", f"{miss[4]:,}"),
    ]))
    print(f"\nPass time {time.time() - t:.1f}s\n")

    # ── US subset, projected, materialised once ────────────────────────────
    t = time.time()
    con.execute(f"""
        CREATE TEMP TABLE us AS
        SELECT code,
               list_filter(product_name, lambda x: x.lang = 'main')[1].text AS name_main,
               list_filter(product_name, lambda x: x.lang = 'en')[1].text   AS name_en,
               list_transform(product_name, lambda x: x.lang)               AS name_langs,
               brands, brands_tags, quantity, product_quantity, product_quantity_unit,
               serving_size, serving_quantity, categories_tags, labels_tags,
               last_modified_t, created_t, completeness, nutriscore_grade,
               (images IS NOT NULL AND len(images) > 0) AS has_images,
               last_image_t, max_imgid, obsolete, lang, scans_n, unique_scans_n, popularity_key
        FROM {src}
        WHERE list_contains(countries_tags, '{US_TAG}')
    """)
    n_us = con.execute("SELECT count(*) FROM us").fetchone()[0]
    print(f"US subset materialised: {n_us:,} rows in {time.time() - t:.1f}s\n")

    # ── 3. fill rates ──────────────────────────────────────────────────────
    def nonempty(expr: str) -> str:
        return f"count(*) FILTER (WHERE {expr})"

    fill_exprs = [
        ("code", "code IS NOT NULL AND code <> ''"),
        ("product_name — any entry", "name_langs IS NOT NULL AND len(name_langs) > 0"),
        ("product_name — `main` text non-empty", "name_main IS NOT NULL AND trim(name_main) <> ''"),
        ("product_name — `en` text non-empty", "name_en IS NOT NULL AND trim(name_en) <> ''"),
        ("product_name — `main` ≠ `en` (both present)", "name_main IS NOT NULL AND name_en IS NOT NULL AND name_main <> name_en"),
        ("brands", "brands IS NOT NULL AND trim(brands) <> ''"),
        ("brands_tags", "brands_tags IS NOT NULL AND len(brands_tags) > 0"),
        ("quantity", "quantity IS NOT NULL AND trim(quantity) <> ''"),
        ("product_quantity", "product_quantity IS NOT NULL AND trim(product_quantity) <> ''"),
        ("product_quantity_unit", "product_quantity_unit IS NOT NULL AND trim(product_quantity_unit) <> ''"),
        ("serving_size", "serving_size IS NOT NULL AND trim(serving_size) <> ''"),
        ("categories_tags", "categories_tags IS NOT NULL AND len(categories_tags) > 0"),
        ("categories_tags — has an `en:` entry", "categories_tags IS NOT NULL AND len(list_filter(categories_tags, lambda x: x LIKE 'en:%')) > 0"),
        ("labels_tags", "labels_tags IS NOT NULL AND len(labels_tags) > 0"),
        ("labels_tags — `en:usda-organic`", "list_contains(labels_tags, 'en:usda-organic')"),
        ("labels_tags — `en:organic`", "list_contains(labels_tags, 'en:organic')"),
        ("last_modified_t", "last_modified_t IS NOT NULL"),
        ("created_t", "created_t IS NOT NULL"),
        ("completeness", "completeness IS NOT NULL"),
        ("nutriscore_grade (skip-list)", "nutriscore_grade IS NOT NULL AND nutriscore_grade <> ''"),
        ("images — non-empty `images` list", "has_images"),
        ("last_image_t", "last_image_t IS NOT NULL"),
        ("max_imgid", "max_imgid IS NOT NULL"),
        ("lang = 'en'", "lang = 'en'"),
    ]
    sql = "SELECT " + ", ".join(nonempty(e) for _, e in fill_exprs) + " FROM us"
    counts = con.execute(sql).fetchone()
    print("## 3. Fill rates on the US subset\n")
    print(md_table(["field", "non-empty", "% of US"], [(f, f"{c:,}", pct(c, n_us)) for (f, _), c in zip(fill_exprs, counts)]))
    print()

    langs = con.execute("""
        SELECT l, count(*) AS n FROM (SELECT unnest(name_langs) AS l FROM us) GROUP BY 1 ORDER BY n DESC LIMIT 12
    """).fetchall()
    print("`product_name` language keys present (top 12, US subset):\n")
    print(md_table(["lang key", "rows"], [(l, f"{n:,}") for l, n in langs]))
    print()

    # ── 4. code shapes ─────────────────────────────────────────────────────
    lens = con.execute(f"""
        SELECT len(code) AS l, count(*) AS n,
               count(*) FILTER (WHERE NOT (code ~ '^[0-9]+$')) AS non_digit,
               count(*) FILTER (WHERE code ~ '^[0-9]+$' AND NOT {sql_gs1_ok('code')}) AS mod10_fail
        FROM us GROUP BY 1 ORDER BY 1
    """).fetchall()
    print("## 4. `code` shapes (US subset)\n")
    print(md_table(["length", "rows", "non-digit", "GS1 mod-10 FAIL (digit-only)"], [(l, f"{n:,}", f"{nd:,}", f"{mf:,}") for l, n, nd, mf in lens]))
    shape = con.execute("""
        SELECT count(*) FILTER (WHERE len(code) = 13 AND code[1] = '0') AS thirteen_lead0,
               count(*) FILTER (WHERE len(code) = 13 AND code[1:2] = '00') AS thirteen_lead00,
               count(*) FILTER (WHERE len(code) = 13 AND code[1] <> '0') AS thirteen_nonzero,
               count(*) FILTER (WHERE len(code) = 13 AND code[1] = '0' AND len(ltrim(code, '0')) = 12) AS exactly_one_lead0,
               count(*) FILTER (WHERE len(code) = 13 AND code[1] = '0' AND len(ltrim(code, '0')) = 8) AS ean8_padded,
               count(*) - count(DISTINCT code) AS duplicate_codes,
               count(*) FILTER (WHERE code IS NULL OR code = '') AS empty_code
        FROM us
    """).fetchone()
    print()
    print(md_table(["metric", "rows"], [
        ("13-digit codes with a leading `0` (= a UPC-A / shorter GTIN spelled EAN-13)", f"**{shape[0]:,}**"),
        ("13-digit codes with leading `00`", f"{shape[1]:,}"),
        ("13-digit codes whose leading `0` strips to EXACTLY 12 digits (UPC-A)", f"**{shape[3]:,}**"),
        ("13-digit codes that strip to 8 digits (EAN-8 padded)", f"{shape[4]:,}"),
        ("13-digit codes with a non-zero first digit (genuine EAN-13)", f"{shape[2]:,}"),
        ("duplicate `code` values (rows − distinct)", f"{shape[5]:,}"),
        ("empty/NULL `code`", f"{shape[6]:,}"),
    ]))
    print("\n(8-digit note: the app also accepts an 8-digit payload whose check digit validates only as a UPC-E expansion; the FAIL column above is the plain EAN-8 mod-10, so a few 8-digit 'fails' can be valid UPC-E.)\n")

    # ── 4b. loadable rows under the app's own canonical form ───────────────
    # barcodeClassifier.ts: digits only, length ∈ {8, 12, 13, 14}, mod-10 OK,
    # then a 13-digit '0…' becomes its 12-digit UPC-A form. The dump has
    # almost no 12-digit spellings, so the 13→12 fold IS the canonicalisation.
    canon = "CASE WHEN len(code) = 13 AND code[1] = '0' THEN code[2:] ELSE code END"
    load = con.execute(f"""
        WITH c AS (
            SELECT {canon} AS canon, name_main, code,
                   (code ~ '^[0-9]+$' AND len(code) IN (8, 12, 13, 14) AND {sql_gs1_ok('code')}) AS gate_ok
            FROM us
        )
        SELECT count(*) FILTER (WHERE gate_ok) AS gate_pass,
               count(*) FILTER (WHERE gate_ok AND name_main IS NOT NULL AND trim(name_main) <> '') AS gate_pass_named,
               count(*) FILTER (WHERE gate_ok AND len(canon) = 12) AS canon_12,
               count(*) FILTER (WHERE gate_ok AND len(canon) = 13) AS canon_13,
               count(*) FILTER (WHERE gate_ok AND len(canon) = 8) AS canon_8,
               count(*) FILTER (WHERE gate_ok AND len(canon) = 14) AS canon_14,
               count(*) FILTER (WHERE gate_ok AND len(canon) = 8 AND code LIKE '0000%') AS canon_8_zero_padded,
               count(*) FILTER (WHERE gate_ok) - count(DISTINCT canon) FILTER (WHERE gate_ok) AS canon_dupes,
               count(*) FILTER (WHERE NOT gate_ok) AS gate_reject
        FROM c
    """).fetchone()
    print("### 4b. Rows that pass the app's barcode gate, by canonical (stored) form\n")
    print(md_table(["metric", "rows"], [
        ("US rows passing the gate (digits, length ∈ {8,12,13,14}, mod-10)", f"**{load[0]:,}**"),
        ("…and with a non-empty main name (= loadable)", f"**{load[1]:,}**"),
        ("canonical 12 digits (UPC-A, the 13→12 fold)", f"{load[2]:,}"),
        ("canonical 13 digits (genuine EAN-13)", f"{load[3]:,}"),
        ("canonical 8 digits (EAN-8 / UPC-E shape)", f"{load[4]:,}"),
        ("…of the 8-digit, spelled `0000…` (zero-padded short codes that happen to pass mod-10)", f"{load[6]:,}"),
        ("canonical 14 digits", f"{load[5]:,}"),
        ("duplicate canonical keys among gate passes", f"{load[7]:,}"),
        ("US rows REJECTED by the gate (bad length or check digit)", f"{load[8]:,}"),
    ]))
    print()

    # ── 5. random rows ─────────────────────────────────────────────────────
    sample = con.execute(f"""
        SELECT code, name_main, brands, quantity, categories_tags[1:4]
        FROM us WHERE name_main IS NOT NULL
        ORDER BY hash(code || '{SAMPLE_SEED}') LIMIT 20
    """).fetchall()
    print("## 5. 20 deterministic random US rows (`ORDER BY hash(code || seed)`)\n")
    print(md_table(["code", "product_name (main)", "brands", "quantity", "categories_tags[0..3]"], sample))
    print()

    # ── 6. vocabularies ────────────────────────────────────────────────────
    units = con.execute("""
        SELECT product_quantity_unit AS u, count(*) AS n FROM us
        WHERE product_quantity_unit IS NOT NULL AND product_quantity_unit <> ''
        GROUP BY 1 ORDER BY n DESC LIMIT 20
    """).fetchall()
    print("## 6. Vocabularies (US subset)\n")
    print("`product_quantity_unit` top 20:\n")
    print(md_table(["unit", "rows"], [(u, f"{n:,}") for u, n in units]))
    qsample = con.execute(f"""
        SELECT quantity FROM us WHERE quantity IS NOT NULL AND trim(quantity) <> ''
        ORDER BY hash(code || '{SAMPLE_SEED}q') LIMIT 25
    """).fetchall()
    print("\n25 random non-empty `quantity` strings: " + ", ".join(f"`{q[0]}`" for q in qsample) + "\n")
    cats = con.execute("""
        SELECT c, count(*) AS n FROM (SELECT unnest(categories_tags) AS c FROM us)
        WHERE c LIKE 'en:%' GROUP BY 1 ORDER BY n DESC LIMIT 30
    """).fetchall()
    print("Top 30 `en:` `categories_tags` (US subset):\n")
    print(md_table(["tag", "rows"], [(c, f"{n:,}") for c, n in cats]))
    grades = con.execute("SELECT count(DISTINCT code) FILTER (WHERE completeness >= 0.5), avg(completeness) FROM us").fetchone()
    print(f"\n`completeness` ≥ 0.5: {grades[0]:,} rows; mean completeness {grades[1]:.3f}\n")

    # ── 7. trimmed size estimate ───────────────────────────────────────────
    size = con.execute("""
        SELECT sum(coalesce(strlen(code),0) + coalesce(strlen(name_main),0)
                 + coalesce(strlen(brands),0) + coalesce(strlen(array_to_string(brands_tags, ',')),0)
                 + coalesce(strlen(quantity),0) + coalesce(strlen(product_quantity),0)
                 + coalesce(strlen(product_quantity_unit),0) + coalesce(strlen(serving_size),0)
                 + coalesce(strlen(array_to_string(categories_tags, ',')),0)
                 + coalesce(strlen(array_to_string(labels_tags, ',')),0) + 16) AS payload_bytes,
               count(*) FILTER (WHERE name_main IS NOT NULL AND trim(name_main) <> '' AND code ~ '^[0-9]+$') AS plausible_rows
        FROM us
    """).fetchone()
    payload = int(size[0] or 0)
    print("## 7. Trimmed-size estimate\n")
    print(md_table(["metric", "value"], [
        ("kept columns (estimate basis)", ", ".join(f"`{c}`" for c in TRIM_COLUMNS)),
        ("US rows", f"{n_us:,}"),
        ("US rows with a digit-only code AND a non-empty main name", f"{size[1]:,}"),
        ("raw payload bytes (sum of text lengths + 16 B for the two epochs)", f"{payload:,} ≈ {payload / 1e6:.0f} MB"),
        ("per-row mean payload", f"{(payload / n_us if n_us else 0):.0f} B"),
        ("Postgres heap estimate (payload × 1.3 + ~40 B row header/alignment)", f"≈ {(payload * 1.3 + 40 * n_us) / 1e6:.0f} MB"),
        ("PK index on a 13-char barcode (~48 B/row)", f"≈ {48 * n_us / 1e6:.0f} MB"),
    ]))
    print("\nStanding decision #8 budget: both trimmed tables ≈ 0.2–0.3 GB (±30%); prod at 0.27 / 2 GB.\n")
    per_col = con.execute("""
        SELECT sum(strlen(coalesce(code, ''))) AS code,
               sum(strlen(coalesce(name_main, ''))) AS name_main,
               sum(strlen(coalesce(brands, ''))) AS brands,
               sum(strlen(coalesce(array_to_string(brands_tags, ','), ''))) AS brands_tags,
               sum(strlen(coalesce(quantity, ''))) AS quantity,
               sum(strlen(coalesce(product_quantity, '')) + strlen(coalesce(product_quantity_unit, ''))) AS product_quantity_pair,
               sum(strlen(coalesce(serving_size, ''))) AS serving_size,
               sum(strlen(coalesce(array_to_string(categories_tags, ','), ''))) AS categories_tags,
               sum(strlen(coalesce(array_to_string(labels_tags, ','), ''))) AS labels_tags
        FROM us
    """).fetchone()
    names = ["code", "name_main", "brands", "brands_tags", "quantity", "product_quantity + unit", "serving_size", "categories_tags", "labels_tags"]
    print("Per-column payload on the US subset (text bytes):\n")
    print(md_table(["column", "MB", "% of payload"], [(n, f"{int(v or 0) / 1e6:.1f}", pct(int(v or 0), payload)) for n, v in zip(names, per_col)]))
    print()

    # ── 8. optional barcode-list hit rate ──────────────────────────────────
    if args.barcodes:
        # First whitespace-separated token per line, so the USDA probe's
        # tab-separated dev_barcodes.txt works as well as a plain list.
        with open(args.barcodes, encoding="utf-8") as fh:
            codes = sorted({ln.split()[0] for ln in fh if ln.strip() and ln.split()[0].isdigit()})
        con.execute("CREATE TEMP TABLE probe_codes (b VARCHAR)")
        con.executemany("INSERT INTO probe_codes VALUES (?)", [(c,) for c in codes])
        # match forms: raw; 0-padded to 13 (OFF's storage form for shorter GTINs); zero-stripped both sides
        hits = con.execute(f"""
            WITH full_dump AS (
                SELECT code, list_contains(countries_tags, '{US_TAG}') AS is_us,
                       list_filter(product_name, lambda x: x.lang = 'main')[1].text AS name_main, brands
                FROM {src}
                WHERE code IN (SELECT b FROM probe_codes)
                   OR code IN (SELECT lpad(b, 13, '0') FROM probe_codes)
                   OR ltrim(code, '0') IN (SELECT ltrim(b, '0') FROM probe_codes)
            )
            SELECT p.b,
                   max(CASE WHEN f.code = p.b THEN 1 ELSE 0 END) AS raw_hit,
                   max(CASE WHEN f.code = lpad(p.b, 13, '0') THEN 1 ELSE 0 END) AS pad13_hit,
                   max(CASE WHEN ltrim(f.code, '0') = ltrim(p.b, '0') THEN 1 ELSE 0 END) AS stripped_hit,
                   max(CASE WHEN f.is_us THEN 1 ELSE 0 END) AS us_hit,
                   string_agg(DISTINCT f.code || ' → ' || coalesce(f.name_main, '∅') || ' / ' || coalesce(f.brands, '∅') || CASE WHEN f.is_us THEN ' [US]' ELSE ' [non-US]' END, '; ') AS rows_hit
            FROM probe_codes p
            LEFT JOIN full_dump f ON f.code = p.b OR f.code = lpad(p.b, 13, '0') OR ltrim(f.code, '0') = ltrim(p.b, '0')
            GROUP BY p.b ORDER BY p.b
        """).fetchall()
        n_codes = len(hits)
        raw_n = sum(h[1] for h in hits)
        pad_n = sum(h[2] for h in hits)
        strip_n = sum(h[3] for h in hits)
        us_n = sum(h[4] for h in hits)
        print(f"## 8. Barcode-list hit rates — `{os.path.basename(args.barcodes)}` ({n_codes} codes)\n")
        print(md_table(["barcode", "raw", "0-padded-13", "zero-stripped", "in US subset", "dump row(s)"],
                       [(b, "HIT" if r else "miss", "HIT" if p_ else "miss", "HIT" if s else "miss", "yes" if u else ("no" if s else "—"), rows or "—")
                        for b, r, p_, s, u, rows in hits]))
        print(f"\nraw {raw_n}/{n_codes} · 0-padded-13 {pad_n}/{n_codes} · zero-stripped {strip_n}/{n_codes} · of which in the US subset {us_n}/{n_codes}\n")

    print(f"---\nWall time: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
