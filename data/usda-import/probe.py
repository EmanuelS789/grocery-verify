#!/usr/bin/env python3
"""
scripts/data-import/usda/probe.py — READ-ONLY probe of the USDA Global Branded
Food Products Database CSV release (FoodData Central "Branded" download,
public domain / CC0) — data-source arc PHASE 3 §1a (2026-09-19).

PURPOSE: the numbers behind the phase-3 design picks, as Markdown: the
         filter funnel (US → not discontinued → the app's barcode gate →
         prefix-2 → has a description), the gtin_upc spellings (12 / 0…13 /
         00…14 / case codes), field fill rates, the package_weight shapes and
         the app-size-grammar parse rate (full string vs FIRST slash token),
         description casing, the branded_food_category vocabulary + a
         PROPOSED map onto the app's 34 canonical categories, the overlap
         with the OFF reference table (the cleaner's load.csv — the rows
         DEV holds), the 48 PROD barcodes against both, a store-brand
         derivation preview, and a trimmed-table size estimate.

HOW:     DuckDB does the join (branded_food.csv ⋈ food.csv on fdc_id), the
         dedupe to the latest publication_date per gtin_upc, the projection
         and every aggregation. EVERY app rule — the barcode gate + canonical
         form, the prefix-2 rule, the size grammar, the store-brand list, the
         category mapper — is run by the APP'S OWN TypeScript in a Deno
         subprocess (probe_rules.ts; standing decision #7), never re-ported
         here. The GS1 mod-10 twin from off/probe.py is used ONLY to label
         why a code failed the gate; it self-tests against the Python port
         before any query runs.

DATA:    argv[1] = the USDA folder (OUTSIDE the repo — never in git). It must
         hold branded_food.csv and food.csv; prod_items.csv (Eman's 2026-09-09
         PROD export) is read when present. --off-load-csv points at the OFF
         cleaner's load.csv (gitignored, scripts/data-import/off/out/) for
         the overlap. Outputs go under --out (default
         scripts/data-import/usda/out/, gitignored): the dedup + rules JSONL,
         usda_categories.csv (the full vocabulary with counts), and the
         report; the PROPOSED map is written to --map (default
         scripts/data-import/usda/usda_category_map.csv — COMMITTED, it is
         the mapping, not data).

DETERMINISM: samples are `ORDER BY hash(gtin || seed)`; ties in the dedupe
         break on modified_date then fdc_id; same inputs → same bytes.

NOT a test, NOT a build step, NOT in `npm run test:all`. No network.

RUN (from the repo root):
  python scripts/data-import/usda/probe.py <usda-folder>
  → prints the report to stdout and writes it to <out>/probe_report.md
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(HERE, "..", "off"))
from probe import md_table, pct, self_test, sql_gs1_ok  # noqa: E402  (off/probe.py)

sys.stdout.reconfigure(encoding="utf-8")

SEED = "20260919"
DEFAULT_OUT = os.path.join(HERE, "out")
DEFAULT_MAP = os.path.join(HERE, "usda_category_map.csv")
DEFAULT_OFF_LOAD = os.path.join(HERE, "..", "off", "out", "load.csv")

# The 34 canonical names (src/constants/categories.ts) — pinned here so the
# proposal can only ever emit a real canonical name; the probe asserts the
# list against the categories the Deno mapper returns.
CANONICAL = [
    "Baby & Toddler", "Baking Supplies", "Bakery & Bread", "Beverages — Alcoholic",
    "Beverages — Non-Alcoholic", "Breakfast & Cereal", "Candy & Chocolate",
    "Canned & Jarred Goods", "Coffee & Tea", "Condiments & Sauces", "Cookies & Crackers",
    "Dairy & Eggs", "Deli & Prepared Foods", "Floral & Plants", "Frozen Foods",
    "Health & Pharmacy", "Household Cleaning", "International Foods", "Kitchen & Home",
    "Laundry", "Meat & Poultry", "Nuts & Dried Fruit", "Oils & Vinegars",
    "Paper & Plastic Goods", "Pasta, Rice & Grains", "Personal Care & Hygiene",
    "Pet Food & Supplies", "Produce", "Seafood", "Snacks & Chips", "Soups & Broths",
    "Spices & Seasonings", "Tobacco & Vaping", "Vitamins & Supplements",
]

# The PROPOSAL generator for usda_category_map.csv (Eman hand-reviews): when
# the app's own mapper finds nothing, the first keyword hit in THIS order
# wins. Order is deliberate — the more specific canonical first (Coffee & Tea
# before the beverage buckets, Cookies & Crackers before Snacks, Frozen
# before anything a frozen category also names). Each entry: (canonical,
# regex over the lowercased USDA category).
KEYWORDS: list[tuple[str, str]] = [
    # non-food first (a food word inside a non-food category name is rare;
    # the reverse — "Plant-Based Milk", "Side Dishes", "Tea Bags" — is not,
    # so the non-food patterns are word-bounded on their weakest keys)
    ("Baby & Toddler", r"\b(baby|infant|toddler)\b"),
    ("Pet Food & Supplies", r"\b(pet|dog|cat|animal)\b"),
    ("Tobacco & Vaping", r"\b(tobacco|cigar|vap)"),
    ("Vitamins & Supplements", r"\b(vitamin|supplement|nutritional|protein powder|meal replacement)"),
    ("Health & Pharmacy", r"\b(health|medic|pharmac|first aid|remed)"),
    ("Personal Care & Hygiene", r"\b(personal care|shampoo|soap|deodorant|oral\b|hygiene|cosmetic|hair\b|lotion)"),
    ("Laundry", r"\b(laundry|fabric|detergent)"),
    ("Household Cleaning", r"\b(clean|household)"),
    ("Paper & Plastic Goods", r"\b(paper|plastic|foil|napkin|tissue)"),
    ("Kitchen & Home", r"\b(kitchen|home\b|cookware|utensil|storage)"),
    ("Floral & Plants", r"\b(floral|flowers?\b|plants\b)"),
    # frozen before everything a frozen category also names (fish, pizza…)
    ("Frozen Foods", r"\b(frozen|ice cream|popsicle)"),
    ("Coffee & Tea", r"\b(coffee|tea|cocoa)\b"),
    ("Oils & Vinegars", r"\b(oils?\b|vinegar)"),
    # "Non Alcoholic …" / "Cooking Wines" must not read as alcoholic (the 2026-09-20 map correction)
    ("Beverages — Alcoholic", r"(?<!non )(?<!non-)\b(beer|wines?\b|spirit|liquor|cider|alcohol|cocktail)"),
    ("Beverages — Non-Alcoholic", r"\b(soda|water\b|juice|drinks?\b|beverage|smoothie|lemonade|nectar)"),
    # the specific pantry buckets before the broad food groups
    ("Spices & Seasonings", r"\b(seasoning|spice|salts?\b|rubs?\b|extract|flavoring|herbs?\b)"),
    ("Condiments & Sauces", r"\b(ketchup|mustard|sauce|dressing|mayo|condiment|gravy|marinade|honey|dips?\b|salsa)"),
    ("Canned & Jarred Goods", r"\b(canned|jarred|pickle|olive|beans?\b|tomato|relish|preserve|jam\b|jell)"),
    ("Soups & Broths", r"\b(soup|broth|stock\b|chili|stew)"),
    ("Cookies & Crackers", r"\b(cookie|biscuit|cracker|wafer)"),
    ("Candy & Chocolate", r"\b(candy|chocolate|gum\b|mints?\b|licorice|marshmallow)"),
    ("Bakery & Bread", r"\b(bread|buns?\b|bagel|rolls\b|tortilla|cake|muffin|pastr|pies?\b|donut|doughnut|croissant|bakery|flatbread|pita)"),
    ("Breakfast & Cereal", r"\b(cereal|oatmeal|granola|pancake|waffle|breakfast|syrup)"),
    ("Snacks & Chips", r"\b(chips?\b|pretzel|snack|popcorn|jerky|puff|crisp)"),
    ("Nuts & Dried Fruit", r"\b(nuts?\b|seeds?\b|dried fruit|trail mix|peanut)"),
    ("Seafood", r"\b(seafood|fish|shrimp|tuna|salmon|crab\b|shellfish|sushi)"),
    ("Meat & Poultry", r"\b(meat|poultry|chicken|beef|pork|turkey|sausage|hot ?dog|bacon|ham\b|lamb|bratwurst|brats)"),
    ("Deli & Prepared Foods", r"\b(deli|prepared|entree|meals?\b|sandwich|salad|pizza|hummus|cold cut|pepperoni|salami|appetizer|hors|lunch)"),
    ("Dairy & Eggs", r"\b(cheese|milk|yogurt|butter|eggs?\b|cream|dairy|kefir|margarine)"),
    ("Produce", r"\b(fruit|vegetable|produce|mushroom|potato|onion)"),
    ("Pasta, Rice & Grains", r"\b(pasta|rice|grain|noodle|quinoa|couscous)"),
    ("Baking Supplies", r"\b(baking|flour|sugar|sweetener|mix|frosting|icing|decor|yeast|starch)"),
    ("International Foods", r"\b(mexican|hispanic|asian|international|ethnic|latin|indian|middle eastern)"),
]
KEYWORD_RES = [(canon, re.compile(rx)) for canon, rx in KEYWORDS]
assert all(c in CANONICAL for c, _ in KEYWORDS)


def keyword_canonical(usda_category: str) -> tuple[str, str] | None:
    low = usda_category.lower()
    for canon, rx in KEYWORD_RES:
        m = rx.search(low)
        if m:
            return canon, m.group(0)
    return None


def deno_exe() -> str:
    found = shutil.which("deno")
    if found:
        return found
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    cand = os.path.join(home, ".deno", "bin", "deno.exe")
    if os.path.isfile(cand):
        return cand
    raise SystemExit("deno not found on PATH (Deno 2.x is required for probe_rules.ts)")


def q(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    return con.execute(sql, params or []).fetchall()


def one(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    return con.execute(sql, params or []).fetchone()[0]


def fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="the USDA release folder (branded_food.csv + food.csv)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--off-load-csv", default=DEFAULT_OFF_LOAD)
    ap.add_argument("--skip-rules", action="store_true", help="reuse an existing usda_rules.jsonl")
    args = ap.parse_args()

    folder = args.folder.replace("\\", "/").rstrip("/")
    out_dir = args.out.replace("\\", "/")
    os.makedirs(out_dir, exist_ok=True)
    branded = f"{folder}/branded_food.csv"
    food = f"{folder}/food.csv"
    for p in (branded, food):
        if not os.path.isfile(p):
            raise SystemExit(f"not a file: {p}")
    prod_items = f"{folder}/prod_items.csv"
    off_load = args.off_load_csv.replace("\\", "/")
    dedup_jsonl = f"{out_dir}/usda_dedup.jsonl"
    rules_jsonl = f"{out_dir}/usda_rules.jsonl"
    categories_csv = f"{out_dir}/usda_categories.csv"
    report_path = f"{out_dir}/probe_report.md"

    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    self_test(con)
    gs1 = sql_gs1_ok  # the labelled-reason helper (self-tested above)

    lines: list[str] = []

    def p(s: str = "") -> None:
        lines.append(s)

    # ── load + join + dedupe ────────────────────────────────────────────────
    con.execute(f"CREATE TABLE bf AS SELECT * FROM read_csv('{branded}', all_varchar = true, header = true)")
    con.execute(f"CREATE TABLE fd AS SELECT fdc_id, description, publication_date, data_type FROM read_csv('{food}', all_varchar = true, header = true)")
    bf_rows = one(con, "SELECT count(*) FROM bf")
    fd_rows = one(con, "SELECT count(*) FROM fd")
    unjoined_b = one(con, "SELECT count(*) FROM bf b LEFT JOIN fd f USING (fdc_id) WHERE f.fdc_id IS NULL")
    unjoined_f = one(con, "SELECT count(*) FROM fd f LEFT JOIN bf b USING (fdc_id) WHERE b.fdc_id IS NULL")
    raw_gtins = one(con, "SELECT count(DISTINCT trim(gtin_upc)) FROM bf")
    empty_gtin = one(con, "SELECT count(*) FROM bf WHERE trim(coalesce(gtin_upc, '')) = ''")
    con.execute("""
        CREATE TABLE dedup AS
        SELECT * EXCLUDE (rn) FROM (
          SELECT trim(b.gtin_upc) AS gtin, b.fdc_id, b.brand_owner, b.brand_name, b.subbrand_name,
                 b.branded_food_category AS category, b.package_weight, b.short_description,
                 f.description, b.market_country, b.discontinued_date, b.data_source,
                 f.publication_date, b.modified_date, b.available_date,
                 row_number() OVER (PARTITION BY trim(b.gtin_upc)
                                    ORDER BY f.publication_date DESC, b.modified_date DESC,
                                             try_cast(b.fdc_id AS BIGINT) DESC) AS rn
          FROM bf b JOIN fd f USING (fdc_id)
          WHERE trim(coalesce(b.gtin_upc, '')) <> ''
        ) WHERE rn = 1
    """)
    dedup_rows = one(con, "SELECT count(*) FROM dedup")
    t_load = time.time() - t0

    # ── the app-rules step (Deno) ───────────────────────────────────────────
    if not (args.skip_rules and os.path.isfile(rules_jsonl)):
        con.execute(f"""
            COPY (SELECT gtin AS g, coalesce(package_weight, '') AS w, coalesce(brand_name, '') AS b,
                         coalesce(brand_owner, '') AS o, coalesce(description, '') AS d,
                         coalesce(category, '') AS c
                  FROM dedup ORDER BY gtin)
            TO '{dedup_jsonl}' (FORMAT JSON)
        """)
        t1 = time.time()
        cmd = [deno_exe(), "run", "--config", "scripts/data-import/deno.json",
               f"--allow-read={out_dir}", f"--allow-write={out_dir}",
               "scripts/data-import/usda/probe_rules.ts", "--in", dedup_jsonl, "--out", rules_jsonl]
        res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        if res.returncode != 0:
            raise SystemExit(f"probe_rules.ts failed:\n{res.stdout}\n{res.stderr}")
        t_rules = time.time() - t1
    else:
        t_rules = 0.0
    con.execute(f"""
        CREATE TABLE rules AS SELECT * FROM read_json('{rules_jsonl}', format = 'newline_delimited',
          columns = {{g: 'VARCHAR', canon: 'VARCHAR', reason: 'VARCHAR', restricted: 'BOOLEAN',
                      shape: 'VARCHAR', fv: 'DOUBLE', fu: 'VARCHAR', tv: 'DOUBLE', tu: 'VARCHAR',
                      sb: 'BOOLEAN', sbo: 'BOOLEAN', cm: 'VARCHAR'}})
    """)
    rules_rows = one(con, "SELECT count(*) FROM rules")
    if rules_rows != dedup_rows:
        raise SystemExit(f"rules rows {rules_rows} != dedup rows {dedup_rows}")
    con.execute("""
        CREATE TABLE d AS
        SELECT x.*, r.canon, r.reason, r.restricted, r.shape, r.fv, r.fu, r.tv, r.tu, r.sb, r.sbo, r.cm,
               x.market_country = 'United States'            AS us_strict,
               x.market_country IN ('United States', 'US')    AS us_any,
               trim(coalesce(x.discontinued_date, '')) = ''   AS not_disc,
               r.canon IS NOT NULL                             AS gate_ok,
               NOT coalesce(r.restricted, false)               AS not_restricted,
               trim(coalesce(x.description, '')) <> ''         AS has_desc
        FROM dedup x JOIN rules r ON r.g = x.gtin
    """)
    # KEPT = the prompt's funnel with the STRICT country spelling.
    con.execute("CREATE TABLE kept AS SELECT * FROM d WHERE us_strict AND not_disc AND gate_ok AND not_restricted AND has_desc")
    # One row per canonical barcode (spelling collisions collapse newest-first).
    con.execute("""
        CREATE TABLE kept1 AS SELECT * EXCLUDE (rn) FROM (
          SELECT *, row_number() OVER (PARTITION BY canon ORDER BY publication_date DESC, modified_date DESC,
                                       try_cast(fdc_id AS BIGINT) DESC) AS rn FROM kept) WHERE rn = 1
    """)
    kept_rows = one(con, "SELECT count(*) FROM kept")
    kept1_rows = one(con, "SELECT count(*) FROM kept1")

    p("# USDA GBFPD probe — `" + os.path.basename(folder) + "`")
    p()
    p(f"- DuckDB {duckdb.__version__}; mod-10 SQL/Python self-test: PASS (off/probe.py's twin, used only to label gate failures)")
    p(f"- every app rule ran in Deno through `probe_rules.ts` (`normalizeScannedBarcode`, `hasRestrictedCirculationPrefix`, the proxy's `mapOffProduct` size grammar, `classifyStoreBrand`, `mapOffCategoriesToCanonical`) over {fmt(rules_rows)} deduped rows")
    p()

    # ── 1. inputs + dedupe ──────────────────────────────────────────────────
    p("## 1. Inputs, join, dedupe")
    p()
    p(md_table(["metric", "value"], [
        ("branded_food.csv data rows", fmt(bf_rows)),
        ("food.csv data rows", fmt(fd_rows)),
        ("branded rows with no food.csv join on fdc_id", fmt(unjoined_b)),
        ("food.csv rows never referenced by branded_food.csv", fmt(unjoined_f)),
        ("branded rows with an EMPTY gtin_upc (dropped before dedupe)", fmt(empty_gtin)),
        ("distinct gtin_upc (raw spelling, trimmed)", fmt(raw_gtins)),
        ("**rows after dedupe to the latest publication_date per gtin_upc**", f"**{fmt(dedup_rows)}**"),
        ("dedupe tie-break", "publication_date DESC, then modified_date DESC, then fdc_id DESC (deterministic)"),
    ]))
    p()
    pub = q(con, "SELECT publication_date, count(*) FROM dedup GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
    p("Top publication_date values on the deduped set: " + ", ".join(f"`{a}` {fmt(b)}" for a, b in pub) + ".")
    p()

    # ── 2. funnel ───────────────────────────────────────────────────────────
    p("## 2. Filter funnel (deduped set → the rows a `ref_products_usda` load would keep)")
    p()
    steps_strict = [
        ("all (deduped)", "true"),
        ("`market_country = 'United States'` (the prompt's spelling)", "us_strict"),
        ("+ `discontinued_date` empty", "us_strict AND not_disc"),
        ("+ barcode passes the app's gate after normalisation (14-digit indicator-0 pre-fold, then `normalizeScannedBarcode`)", "us_strict AND not_disc AND gate_ok"),
        ("+ prefix-2 excluded (`hasRestrictedCirculationPrefix` on the canonical form, standing #11)", "us_strict AND not_disc AND gate_ok AND not_restricted"),
        ("+ has a `description`", "us_strict AND not_disc AND gate_ok AND not_restricted AND has_desc"),
    ]
    rows = []
    prev = None
    for label, cond in steps_strict:
        n = one(con, f"SELECT count(*) FROM d WHERE {cond}")
        n_any = one(con, f"SELECT count(*) FROM d WHERE {cond.replace('us_strict', 'us_any')}")
        rows.append((label, fmt(n), fmt(n_any), "" if prev is None else f"−{fmt(prev - n)}"))
        prev = n
    rows.append(("**distinct canonical barcodes among the kept rows = the table's row count**", f"**{fmt(kept1_rows)}**",
                 fmt(one(con, "SELECT count(DISTINCT canon) FROM d WHERE us_any AND not_disc AND gate_ok AND not_restricted AND has_desc")),
                 f"−{fmt(kept_rows - kept1_rows)} spelling collisions (same product under 12 / 0…13 / 00…14)"))
    p(md_table(["step", "rows (strict `'United States'`)", "rows (`'United States'` OR `'US'`)", "dropped"], rows))
    p()
    mc = q(con, "SELECT coalesce(market_country, '(blank)'), count(*) FROM dedup GROUP BY 1 ORDER BY 2 DESC")
    p("`market_country` on the deduped set: " + ", ".join(f"`{a}` {fmt(b)}" for a, b in mc)
      + ". **Premise check:** the release spells the US market two ways — `'United States'` and `'US'`; the prompt's equality filter keeps only the first. The rest of this report uses the STRICT spelling (the prompt's); the third column shows what `'US'` would add at each step.")
    p()
    disc_note = one(con, "SELECT count(*) FROM d WHERE us_strict AND NOT not_disc")
    p(f"Discontinued rows dropped inside the strict US set: {fmt(disc_note)} (the `discontinued_date` column is a free date string; any non-empty value counts).")
    p()

    # gate-failure reasons on the US + not-discontinued set
    p("### 2b. Why rows fail the gate (strict US, not discontinued)")
    p()
    reason_rows = q(con, f"""
        SELECT CASE
                 WHEN reason = 'case_code' THEN '14-digit with a NON-ZERO packaging indicator (case / logistics code) — EXCLUDED by rule'
                 WHEN NOT (gtin ~ '^[0-9]+$') THEN 'non-digit characters'
                 WHEN len(gtin) NOT IN (8, 12, 13, 14) THEN 'length not in {{8, 12, 13, 14}} (' || CASE WHEN len(gtin) < 8 THEN 'shorter than 8' WHEN len(gtin) BETWEEN 9 AND 11 THEN '9–11' ELSE '15+' END || ')'
                 WHEN len(gtin) = 14 THEN '14-digit, indicator 0, body fails the GS1 check digit'
                 ELSE 'fails the GS1 check digit (' || len(gtin) || ' digits)'
               END AS why, count(*) AS n
        FROM d WHERE us_strict AND not_disc AND NOT gate_ok
        GROUP BY 1 ORDER BY 2 DESC
    """)
    p(md_table(["reason", "rows"], [(a, fmt(b)) for a, b in reason_rows]))
    p()
    eleven = one(con, "SELECT count(*) FROM d WHERE us_strict AND not_disc AND gtin ~ '^[0-9]{11}$'")
    eleven_fix = one(con, "SELECT count(*) FROM d WHERE us_strict AND not_disc AND gtin ~ '^[0-9]{11}$' AND " + gs1("('0' || gtin)"))
    p(f"Observation (not a rule): of the {fmt(eleven)} eleven-digit values in that set, {fmt(eleven_fix)} pass the GS1 check when a leading `0` is prepended — a dropped-leading-zero shape (spreadsheet export), i.e. a UPC-A under a spelling the gate cannot accept; and a further check with the check digit appended instead is the phase-4 reconstruction question, not phase 3's. Recorded for phase 4a; excluded here.")
    p()

    # ── 3. spellings ────────────────────────────────────────────────────────
    p("## 3. `gtin_upc` spellings (deduped set)")
    p()
    lens = q(con, f"""
        SELECT len(gtin), count(*),
               count(*) FILTER (WHERE NOT (gtin ~ '^[0-9]+$')),
               count(*) FILTER (WHERE gtin ~ '^[0-9]+$' AND NOT {gs1('gtin')})
        FROM dedup GROUP BY 1 ORDER BY 1
    """)
    p(md_table(["length", "rows", "non-digit", "GS1 mod-10 fails (digit-only)"], [(a, fmt(b), fmt(c), fmt(e)) for a, b, c, e in lens]))
    p()
    sp = q(con, f"""
        SELECT spelling, count(*), count(*) FILTER (WHERE ok) FROM (
          SELECT CASE
                   WHEN gtin ~ '^00[0-9]{{12}}$' THEN '14-digit `00…` (indicator 0 + a `0…` EAN-13 body = a UPC-A) → 12'
                   WHEN gtin ~ '^0[1-9][0-9]{{12}}$' THEN '14-digit `0…` with a NON-ZERO 13-digit body (indicator 0 + EAN-13) → 13 (not in the prompt''s list; the pre-fold rule covers it)'
                   WHEN gtin ~ '^[1-9][0-9]{{13}}$' THEN '14-digit with a NON-ZERO indicator (case code) → EXCLUDE'
                   WHEN gtin ~ '^0[0-9]{{12}}$' THEN '13-digit `0…` (a UPC-A spelled EAN) → 12'
                   WHEN gtin ~ '^[1-9][0-9]{{12}}$' THEN '13-digit non-zero (EAN-13) → 13'
                   WHEN gtin ~ '^[0-9]{{12}}$' THEN '12-digit (UPC-A) → 12'
                   WHEN gtin ~ '^[0-9]{{8}}$' THEN '8-digit (EAN-8 / UPC-E) → 8'
                   ELSE 'other length or non-digit → rejected by the gate'
                 END AS spelling,
                 canon IS NOT NULL AS ok
          FROM d) GROUP BY 1 ORDER BY 2 DESC
    """)
    p(md_table(["spelling → canonical form", "rows", "of which pass the gate"], [(a, fmt(b), fmt(c)) for a, b, c in sp]))
    p()
    upce = one(con, f"SELECT count(*) FROM d WHERE gtin ~ '^[0-9]{{8}}$' AND canon IS NOT NULL AND NOT {gs1('gtin')}")
    p(f"8-digit values that fail the EAN-8 check but pass as a zero-suppressed UPC-E (the gate's expansion rule): {fmt(upce)}.")
    p()
    coll = q(con, """
        SELECT n_spellings, count(*) FROM (
          SELECT canon, count(*) AS n_spellings FROM d WHERE canon IS NOT NULL GROUP BY canon) GROUP BY 1 ORDER BY 1
    """)
    p("Canonical-form collisions on the whole gated set (one product under several spellings): " + ", ".join(f"{fmt(b)} canonical codes with {a} spelling{'s' if a > 1 else ''}" for a, b in coll) + ". The cleaner must dedupe AGAIN on the canonical form after the fold (newest publication wins), exactly as the funnel's last line does.")
    p()

    # ── 4. fill rates ───────────────────────────────────────────────────────
    p("## 4. Field fill rates")
    p()
    fields = ["brand_name", "brand_owner", "subbrand_name", "category", "package_weight", "short_description", "description"]
    rows = []
    for f in fields:
        a = one(con, f"SELECT count(*) FROM dedup WHERE trim(coalesce({f}, '')) <> ''")
        b = one(con, f"SELECT count(*) FROM kept1 WHERE trim(coalesce({f}, '')) <> ''")
        label = "branded_food_category" if f == "category" else f
        rows.append((label, fmt(a), pct(a, dedup_rows), fmt(b), pct(b, kept1_rows)))
    p(md_table(["field", "deduped: non-empty", "%", "kept (table rows): non-empty", "%"], rows))
    p()
    bboth = one(con, "SELECT count(*) FROM kept1 WHERE trim(coalesce(brand_name, '')) = '' AND trim(coalesce(brand_owner, '')) <> ''")
    bnone = one(con, "SELECT count(*) FROM kept1 WHERE trim(coalesce(brand_name, '')) = '' AND trim(coalesce(brand_owner, '')) = ''")
    p(f"Brand fallback on the kept set: `brand_name` empty but `brand_owner` present → {fmt(bboth)} rows ({pct(bboth, kept1_rows)}) gain a brand from the fallback; neither → {fmt(bnone)} rows ({pct(bnone, kept1_rows)}) stay brandless.")
    p()

    # ── 5. package_weight ───────────────────────────────────────────────────
    p("## 5. `package_weight` — 25 random raw values, shapes, and the app-size-grammar parse rate")
    p()
    sample25 = q(con, f"SELECT package_weight FROM kept1 WHERE trim(coalesce(package_weight, '')) <> '' ORDER BY hash(gtin || '{SEED}') LIMIT 25")
    p("25 random non-empty raw values (kept set, `ORDER BY hash(gtin || seed)`):")
    p()
    for (v,) in sample25:
        p(f"- `{v}`")
    p()
    con.execute(f"CREATE TABLE s5000 AS SELECT * FROM kept1 ORDER BY hash(gtin || '{SEED}2') LIMIT 5000")
    for tbl, title in (("s5000", "5,000-row sample"), ("kept1", "every kept row")):
        n = one(con, f"SELECT count(*) FROM {tbl}")
        shp = q(con, f"""
            SELECT shape, count(*), count(*) FILTER (WHERE fv IS NOT NULL), count(*) FILTER (WHERE tv IS NOT NULL)
            FROM {tbl} GROUP BY 1 ORDER BY 2 DESC
        """)
        full_ok = one(con, f"SELECT count(*) FROM {tbl} WHERE fv IS NOT NULL")
        first_ok = one(con, f"SELECT count(*) FROM {tbl} WHERE tv IS NOT NULL")
        nonempty = one(con, f"SELECT count(*) FROM {tbl} WHERE shape <> 'empty'")
        p(f"### 5{'a' if tbl == 's5000' else 'b'}. Parse rate on the {title} ({fmt(n)} rows; {fmt(nonempty)} with a non-empty `package_weight`)")
        p()
        p(md_table(["shape of the raw string", "rows", "parses through the FULL string (the OFF cleaner's grammar as-is)", "parses through the FIRST slash token"],
                   [(a, fmt(b), f"{fmt(c)} ({pct(c, b)})", f"{fmt(e)} ({pct(e, b)})") for a, b, c, e in shp]))
        p()
        p(f"**Totals:** full string {fmt(full_ok)} / {fmt(n)} = {pct(full_ok, n)} of rows ({pct(full_ok, nonempty)} of non-empty); "
          f"first token {fmt(first_ok)} / {fmt(n)} = {pct(first_ok, n)} of rows ({pct(first_ok, nonempty)} of non-empty).")
        p()
    units = q(con, "SELECT tu, count(*) FROM kept1 WHERE tu IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")
    p("Units produced by the first-token parse (kept set): " + ", ".join(f"`{a}` {fmt(b)}" for a, b in units) + ".")
    p()
    unparsed = q(con, f"SELECT package_weight FROM kept1 WHERE shape <> 'empty' AND tv IS NULL ORDER BY hash(gtin || '{SEED}3') LIMIT 15")
    p("15 random non-empty values the first-token rule still does NOT parse:")
    p()
    for (v,) in unparsed:
        p(f"- `{v}`")
    p()

    # ── 6. description casing ───────────────────────────────────────────────
    p("## 6. `description` casing and brand-in-description (kept set)")
    p()
    caps = one(con, "SELECT count(*) FROM kept1 WHERE description = upper(description) AND regexp_matches(description, '[A-Za-z]')")
    lower_all = one(con, "SELECT count(*) FROM kept1 WHERE description = lower(description) AND regexp_matches(description, '[A-Za-z]')")
    brand_in = one(con, "SELECT count(*) FROM kept1 WHERE trim(coalesce(brand_name, '')) <> '' AND contains(lower(description), lower(trim(brand_name)))")
    brand_has = one(con, "SELECT count(*) FROM kept1 WHERE trim(coalesce(brand_name, '')) <> ''")
    owner_in = one(con, "SELECT count(*) FROM kept1 WHERE trim(coalesce(brand_name, '')) = '' AND trim(coalesce(brand_owner, '')) <> '' AND contains(lower(description), lower(trim(brand_owner)))")
    either_in = one(con, """SELECT count(*) FROM kept1 WHERE
        (trim(coalesce(brand_name, '')) <> '' AND contains(lower(description), lower(trim(brand_name))))
        OR (trim(coalesce(brand_name, '')) = '' AND trim(coalesce(brand_owner, '')) <> '' AND contains(lower(description), lower(trim(brand_owner))))""")
    comma_lead = one(con, "SELECT count(*) FROM kept1 WHERE trim(coalesce(brand_name, '')) <> '' AND starts_with(lower(description), lower(trim(brand_name)) || ',')")
    p(md_table(["metric", "rows", "% of kept"], [
        ("description ALL-CAPS (no lowercase letter)", fmt(caps), pct(caps, kept1_rows)),
        ("description all-lowercase", fmt(lower_all), pct(lower_all, kept1_rows)),
        ("brand_name appears inside the description (case-insensitive substring; of rows with a brand_name)", fmt(brand_in), pct(brand_in, brand_has)),
        ("…of which the description STARTS with `<brand_name>,` (the 'BRAND, PRODUCT' USDA form)", fmt(comma_lead), pct(comma_lead, brand_has)),
        ("brand_owner appears inside the description when brand_name is empty", fmt(owner_in), pct(owner_in, kept1_rows)),
        ("brand string (name, else owner) appears inside the description — all kept rows", fmt(either_in), pct(either_in, kept1_rows)),
    ]))
    p()
    ex = q(con, f"SELECT description, brand_name, brand_owner FROM kept1 ORDER BY hash(gtin || '{SEED}4') LIMIT 12")
    p("12 random kept rows (description · brand_name · brand_owner):")
    p()
    for dsc, bn, bo in ex:
        p(f"- `{dsc}` · `{bn or ''}` · `{bo or ''}`")
    p()

    # ── 7. categories ───────────────────────────────────────────────────────
    p("## 7. `branded_food_category` — vocabulary and the PROPOSED map")
    p()
    cats = q(con, "SELECT coalesce(category, ''), count(*), any_value(cm) FROM kept1 GROUP BY 1 ORDER BY 2 DESC, 1")
    distinct_all = one(con, "SELECT count(DISTINCT coalesce(category, '')) FROM dedup")
    with open(categories_csv, "w", encoding="utf-8", newline="") as f:
        f.write("usda_category,rows_kept\n")
        for c, n, _ in cats:
            f.write('"' + c.replace('"', '""') + f'",{n}\n')
    mapper_n = keyword_n = unmapped_n = 0
    mapper_rows = keyword_rows = unmapped_rows = 0
    map_rows: list[tuple[str, int, str, str, str]] = []
    for c, n, cm in cats:
        if c == "":
            map_rows.append(("", n, "", "unmapped", "blank category"))
            unmapped_n += 1
            unmapped_rows += n
            continue
        if cm:
            assert cm in CANONICAL, cm
            map_rows.append((c, n, cm, "auto", "app mapper (mapOffCategoriesToCanonical)"))
            mapper_n += 1
            mapper_rows += n
            continue
        kw = keyword_canonical(c)
        if kw:
            map_rows.append((c, n, kw[0], "auto", f"keyword '{kw[1]}'"))
            keyword_n += 1
            keyword_rows += n
        else:
            map_rows.append((c, n, "", "unmapped", ""))
            unmapped_n += 1
            unmapped_rows += n
    with open(args.map, "w", encoding="utf-8", newline="") as f:
        f.write("usda_category,rows_kept,canonical,status,rule\n")
        for c, n, canon, status, rule in map_rows:
            f.write(",".join('"' + s.replace('"', '""') + '"' if isinstance(s, str) else str(s) for s in (c, n, canon, status, rule)) + "\n")
    p(md_table(["metric", "value"], [
        ("distinct `branded_food_category` values (deduped set, blank counted)", fmt(distinct_all)),
        ("distinct values on the kept set (= rows of the map CSV)", fmt(len(cats))),
        ("mapped `auto` by the app's own mapper (`mapOffCategoriesToCanonical` on the category string)", f"{fmt(mapper_n)} categories / {fmt(mapper_rows)} rows ({pct(mapper_rows, kept1_rows)})"),
        ("mapped `auto` by the probe's keyword table (proposal only — Eman reviews)", f"{fmt(keyword_n)} categories / {fmt(keyword_rows)} rows ({pct(keyword_rows, kept1_rows)})"),
        ("`unmapped` (blank canonical — the row loads with `category_id` NULL)", f"{fmt(unmapped_n)} categories / {fmt(unmapped_rows)} rows ({pct(unmapped_rows, kept1_rows)})"),
        ("full vocabulary with counts", f"`{os.path.relpath(categories_csv, REPO_ROOT).replace(os.sep, '/')}` (gitignored)"),
        ("PROPOSED map (COMMITTED)", f"`{os.path.relpath(args.map, REPO_ROOT).replace(os.sep, '/')}` — columns `usda_category,rows_kept,canonical,status,rule`"),
    ]))
    p()
    p("Top 40 categories with the proposal (rows = kept rows):")
    p()
    p(md_table(["#", "usda_category", "rows", "proposed canonical", "status", "rule"],
               [(i + 1, c, fmt(n), canon, status, rule) for i, (c, n, canon, status, rule) in enumerate(map_rows[:40])]))
    p()
    canon_counts: dict[str, int] = {}
    for c, n, canon, status, _ in map_rows:
        if status == "auto":
            canon_counts[canon] = canon_counts.get(canon, 0) + n
    p("Rows per proposed canonical category (the load's `category_id` distribution if the proposal stands):")
    p()
    p(md_table(["canonical", "rows"], [(k, fmt(v)) for k, v in sorted(canon_counts.items(), key=lambda kv: (-kv[1], kv[0]))]))
    p()
    p("Top 30 `unmapped` categories by rows:")
    p()
    p(md_table(["usda_category", "rows"], [(c, fmt(n)) for c, n, _, status, _ in map_rows if status == "unmapped"][:30]))
    p()

    # ── 8. overlap with the OFF table ───────────────────────────────────────
    p("## 8. Overlap with `ref_products_off` (the OFF cleaner's `load.csv` = the rows DEV holds, minus DEV's live-cached rows)")
    p()
    if os.path.isfile(off_load):
        con.execute(f"CREATE TABLE off AS SELECT barcode FROM read_csv('{off_load}', all_varchar = true, header = true)")
        off_rows = one(con, "SELECT count(*) FROM off")
        present = one(con, "SELECT count(*) FROM kept1 k JOIN off o ON o.barcode = k.canon")
        absent = kept1_rows - present
        absent_sb = one(con, "SELECT count(*) FROM kept1 k LEFT JOIN off o ON o.barcode = k.canon WHERE o.barcode IS NULL AND k.sb")
        present_sb = one(con, "SELECT count(*) FROM kept1 k JOIN off o ON o.barcode = k.canon WHERE k.sb")
        us_add_absent = one(con, """SELECT count(*) FROM (
            SELECT DISTINCT canon FROM d WHERE us_any AND NOT us_strict AND not_disc AND gate_ok AND not_restricted AND has_desc) x
            LEFT JOIN off o ON o.barcode = x.canon LEFT JOIN kept1 k ON k.canon = x.canon WHERE o.barcode IS NULL AND k.canon IS NULL""")
        p(md_table(["metric", "value"], [
            ("OFF `load.csv` rows (canonical barcodes)", fmt(off_rows)),
            ("kept USDA canonical barcodes", fmt(kept1_rows)),
            ("**present in the OFF table** (both sources know the barcode)", f"**{fmt(present)}** ({pct(present, kept1_rows)})"),
            ("**absent from the OFF table = USDA's actual contribution**", f"**{fmt(absent)}** ({pct(absent, kept1_rows)})"),
            ("…of the absent: flagged store-brand by the app's `classifyStoreBrand` (name + brand)", f"{fmt(absent_sb)} ({pct(absent_sb, absent)})"),
            ("…of the present: flagged store-brand the same way", f"{fmt(present_sb)} ({pct(present_sb, present)})"),
            ("the `'US'`-spelled rows (dropped by the strict filter) that would ALSO be absent from OFF", fmt(us_add_absent)),
        ]))
        p()
        p("20 random ABSENT rows (`ORDER BY hash(canon || seed)`; canonical · raw gtin · description · brand_name · brand_owner · category · package_weight):")
        p()
        samp = q(con, f"""SELECT k.canon, k.gtin, k.description, k.brand_name, k.brand_owner, k.category, k.package_weight
                          FROM kept1 k LEFT JOIN off o ON o.barcode = k.canon WHERE o.barcode IS NULL
                          ORDER BY hash(k.canon || '{SEED}5') LIMIT 20""")
        p(md_table(["canonical", "raw", "description", "brand_name", "brand_owner", "category", "package_weight"],
                   [tuple("" if v is None else v for v in r) for r in samp]))
        p()
        # The DEV verification inputs (the session runs read-only SELECTs
        # against the hosted table with these lists): a 1,000-barcode random
        # sample of the kept set with its LOCAL present-count, and the 20
        # absent rows above (expected 0 on DEV).
        sample1000 = [r[0] for r in q(con, f"SELECT canon FROM kept1 ORDER BY hash(canon || '{SEED}6') LIMIT 1000")]
        local_present = one(con, "SELECT count(*) FROM (SELECT unnest(?::VARCHAR[]) AS c) s JOIN off o ON o.barcode = s.c", [sample1000])
        with open(f"{out_dir}/dev_check_sample_1000.txt", "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(sample1000) + "\n")
        with open(f"{out_dir}/dev_check_absent_20.txt", "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(r[0] for r in samp) + "\n")
        p(f"DEV verification inputs written: `out/dev_check_sample_1000.txt` (1,000 random kept barcodes; **{fmt(local_present)} of them present in `load.csv` locally** — the hosted count must match) and `out/dev_check_absent_20.txt` (the 20 rows above; the hosted count must be 0).")
        p()
    else:
        p(f"OFF load.csv not found at `{off_load}` — overlap skipped.")
        p()

    # ── 9. the 48 PROD barcodes ─────────────────────────────────────────────
    p("## 9. The 48 PROD barcodes (Eman's 2026-09-09 `prod_items.csv`) against the kept USDA set and the OFF table")
    p()
    if os.path.isfile(prod_items):
        con.execute(f"CREATE TABLE prod AS SELECT * FROM read_csv('{prod_items}', all_varchar = true, header = true)")
        has_off = one(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'off'") == 1
        off_join = "LEFT JOIN off o ON o.barcode = p.barcode" if has_off else ""
        off_col = "o.barcode IS NOT NULL" if has_off else "false"
        pr = q(con, f"""
            SELECT p.barcode, p.name, p.brand, p.source,
                   k.canon IS NOT NULL AS usda_hit, {off_col} AS off_hit,
                   k.description, k.brand_name, k.brand_owner, k.category, k.sb
            FROM prod p LEFT JOIN kept1 k ON k.canon = p.barcode {off_join}
            ORDER BY p.barcode
        """)
        both = sum(1 for r in pr if r[4] and r[5])
        usda_only = [r for r in pr if r[4] and not r[5]]
        off_only = sum(1 for r in pr if r[5] and not r[4])
        neither = sum(1 for r in pr if not r[4] and not r[5])
        p(md_table(["metric", "count"], [
            ("both tables", both), ("OFF only", off_only), ("**USDA only**", f"**{len(usda_only)}**"), ("neither", neither),
        ]))
        p()
        p("USDA-only rows: " + ("; ".join(f"`{r[0]}` {r[1]} / {r[2]} (source `{r[3]}`) → USDA `{r[6]}` / brand `{r[7] or ''}` / owner `{r[8] or ''}` / `{r[9]}` / store-brand by the app's list: {r[10]}" for r in usda_only) or "none") + ".")
        p()
        p("Per-bucket numbers are §5's (they need the loaded DEV table and §C2's bucket rules); this is the funnel check that the kept set still carries the expected rows.")
        p()
    else:
        p("prod_items.csv absent — skipped.")
        p()

    # ── 10. store-brand derivation preview ──────────────────────────────────
    p("## 10. `is_store_brand` derivation preview (kept set)")
    p()
    app_sb = one(con, "SELECT count(*) FROM kept1 WHERE sb")
    owner_sb = one(con, "SELECT count(*) FROM kept1 WHERE sbo")
    OWNERS = ["Wegmans", "Market Basket", "DeMoulas", "Hannaford", "Ahold", "Stop & Shop", "Shaw's", "Albertsons",
              "Safeway", "Trader Joe", "Aldi", "Wal-Mart", "Walmart", "Target", "Costco", "Whole Foods", "BJ's",
              "Kroger", "Meijer", "Topco", "Hy-Vee", "Giant Eagle", "Wakefern", "Harris-Teeter", "Weis Markets",
              "Schnuck", "Raley's", "Tops Markets", "Smart & Final", "Publix", "H-E-B", "HEB", "Dollar General",
              "Walgreen", "CVS", "Amazon", "Sprouts", "Lidl", "Food Lion", "Winco", "Save Mart", "Price Chopper",
              "Big Y", "ShopRite", "Fresh Market", "Southeastern Grocers", "SpartanNash", "Supervalu", "Associated Wholesale"]
    rows = []
    cond_parts = []
    for o in OWNERS:
        cond = f"lower(coalesce(brand_owner, '')) LIKE '%{o.lower().replace(chr(39), chr(39) * 2)}%'"
        cond_parts.append(cond)
        n = one(con, f"SELECT count(*) FROM kept1 WHERE {cond}")
        extra = one(con, f"SELECT count(*) FROM kept1 WHERE {cond} AND NOT sb")
        if n:
            rows.append((o, fmt(n), fmt(extra)))
    any_owner = " OR ".join(cond_parts)
    owner_any = one(con, f"SELECT count(*) FROM kept1 WHERE {any_owner}")
    union = one(con, f"SELECT count(*) FROM kept1 WHERE sb OR ({any_owner})")
    p(md_table(["metric", "rows", "% of kept"], [
        ("the app's `classifyStoreBrand(description, brand)` as it stands (KNOWN_STORE_BRANDS token match)", fmt(app_sb), pct(app_sb, kept1_rows)),
        ("the app's list applied to `brand_owner` alone", fmt(owner_sb), pct(owner_sb, kept1_rows)),
        ("a retailer-OWNER list (below; `brand_owner` contains, case-insensitive) — the census the prompt names", fmt(owner_any), pct(owner_any, kept1_rows)),
        ("**union: app rule OR owner list — the proposed `is_store_brand` at load**", f"**{fmt(union)}**", pct(union, kept1_rows)),
    ]))
    p()
    p("Owner strings with hits (rows; of which the app's list does NOT already flag) — a provisional list built from the 2026-09-09 §C3 census + the top-25 `brand_owner` retailers; false matches are possible (e.g. `Shaw's` inside a manufacturer name) and Eman trims it at the pick:")
    p()
    p(md_table(["owner string", "kept rows", "not flagged by the app's list"], rows))
    p()

    # ── 11. size estimate ───────────────────────────────────────────────────
    p("## 11. Estimated `ref_products_usda` size after trimming")
    p()
    est = q(con, """
        SELECT avg(strlen(canon)), avg(strlen(gtin)), avg(strlen(coalesce(description, ''))),
               avg(strlen(coalesce(nullif(trim(brand_name), ''), nullif(trim(brand_owner), ''), ''))),
               avg(strlen(coalesce(brand_owner, ''))), avg(strlen(coalesce(category, ''))),
               avg(strlen(coalesce(package_weight, ''))),
               count(*) FILTER (WHERE tv IS NOT NULL)
        FROM kept1
    """)[0]
    canon_b, gtin_b, name_b, brand_b, owner_b, cat_b, pw_b, size_n = est
    cat_n = mapper_rows + keyword_rows  # the proposal's coverage (section 7)
    # Postgres row: 24-byte header + 4-byte line pointer; text = 1-byte header + bytes (short varlena);
    # numeric ≈ 10; text unit ≈ 4; uuid 16; bigint 8; date 4; bool 1; timestamptz 8; alignment padding ≈ 8.
    per_row = 28 + (canon_b + 1) + (gtin_b + 1) + (name_b + 1) + (brand_b + 1) + (owner_b + 1) + (cat_b + 1) \
        + (pw_b + 1) + 10 * (size_n / kept1_rows) + 4 * (size_n / kept1_rows) + 16 * (cat_n / kept1_rows) + 8 + 4 + 1 + 8 + 8
    heap = per_row * kept1_rows
    index = (canon_b + 8 + 16) * kept1_rows * 1.4
    p(md_table(["column (kept set averages)", "avg bytes"], [
        ("barcode (canonical)", f"{canon_b:.1f}"), ("gtin_upc_raw", f"{gtin_b:.1f}"), ("name (description)", f"{name_b:.1f}"),
        ("brand (brand_name, else brand_owner)", f"{brand_b:.1f}"), ("brand_owner", f"{owner_b:.1f}"),
        ("usda_category", f"{cat_b:.1f}"), ("raw package_weight (if kept, cf. OFF's off_quantity)", f"{pw_b:.1f}"),
        ("size pair present", f"{fmt(size_n)} rows"), ("category_id present (proposal)", f"{fmt(cat_n)} rows"),
    ]))
    p()
    p(f"**Estimate:** ≈ {per_row:.0f} bytes/row × {fmt(kept1_rows)} rows ≈ **{heap / 1e6:.0f} MB heap + ≈ {index / 1e6:.0f} MB PK index ≈ {(heap + index) / 1e6:.0f} MB** "
      f"(OFF measured 217 B/row all-in for 1.085 M rows = 236 MB; the same per-row overhead assumptions are used here). Without the raw `package_weight` column: ≈ {(heap - (pw_b + 1) * kept1_rows + index) / 1e6:.0f} MB.")
    p()

    # ── 12. timing ──────────────────────────────────────────────────────────
    p("## 12. Run")
    p()
    p(f"Wall time: load + join + dedupe {t_load:.1f} s; Deno app-rules step {t_rules:.1f} s; total {time.time() - t0:.1f} s. Outputs: `{os.path.relpath(out_dir, REPO_ROOT).replace(os.sep, '/')}/` (gitignored) + the map CSV.")

    report = "\n".join(lines) + "\n"
    with open(report_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    main()
