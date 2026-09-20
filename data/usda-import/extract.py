#!/usr/bin/env python3
"""
scripts/data-import/usda/extract.py — STEP 1 of the USDA import: CSV release → JSONL

PURPOSE: Pull the rows and columns the TypeScript cleaner (clean.ts) needs
         out of USDA FoodData Central's Branded Foods CSV release (public
         domain / CC0 — branded_food.csv ⋈ food.csv on fdc_id) into a
         newline-delimited JSON file under scripts/data-import/usda/out/
         (gitignored). DuckDB does ONLY what SQL is good at — the join, the
         dedupe to one row per RAW gtin_upc spelling, the market / discontinued
         filter and the column projection; every normalisation decision
         (the 14-digit pre-fold, the barcode gate, the canonical dedupe, brand,
         casing, size, category, is_store_brand) is made by clean.ts with the
         app's own code, never here.

THE ROW FILTER (phase-3 gate-1 picks, 2026-09-20):
         market_country IN ('United States', 'US') — the release spells the
         US market both ways — AND discontinued_date empty.

THE SPELLING DEDUPE: one row per trimmed gtin_upc, the LATEST food.csv
         publication_date winning (ties: modified_date DESC, fdc_id DESC).
         The same product also appears under several SPELLINGS (12 / 0…13 /
         00…14); collapsing those needs the canonical form, which only the
         app's gate can produce — clean.ts does it (newest publication wins
         again).

DETERMINISM: the full extract is ORDER BY gtin; --sample N takes N rows in
         hash(gtin || seed) order (a fixed, reproducible pseudo-random sample
         — the §3 dry run uses 1,000).

INPUTS / OUTPUTS (all outside git):
  argv[1]        the release folder (branded_food.csv + food.csv inside)
  --out <path>   JSONL to write (default scripts/data-import/usda/out/usda_us.jsonl)
  --sample N     write only N rows (deterministic sample) — the dry run

RUN:
  python scripts/data-import/usda/extract.py <folder> --sample 1000
  python scripts/data-import/usda/extract.py <folder>                # full

NOT a test, NOT a build step, NOT in `npm run test:all`. No network.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

sys.stdout.reconfigure(encoding="utf-8")

SAMPLE_SEED = "20260920"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "usda_us.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--sample", type=int, default=None)
    args = ap.parse_args()

    folder = args.folder.replace("\\", "/").rstrip("/")
    branded = f"{folder}/branded_food.csv"
    food = f"{folder}/food.csv"
    for p in (branded, food):
        if not os.path.isfile(p):
            raise SystemExit(f"not a file: {p}")
    out = args.out.replace("\\", "/")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")

    order = f"ORDER BY hash(gtin || '{SAMPLE_SEED}') LIMIT {int(args.sample)}" if args.sample else "ORDER BY gtin"
    t0 = time.time()
    con.execute(f"""
        COPY (
          SELECT gtin, fdc_id, brand_owner, brand_name, category, package_weight, description,
                 publication_date, modified_date, market_country
          FROM (
            SELECT trim(b.gtin_upc)                       AS gtin,
                   try_cast(b.fdc_id AS BIGINT)           AS fdc_id,
                   b.brand_owner, b.brand_name,
                   b.branded_food_category                AS category,
                   b.package_weight,
                   f.description,
                   f.publication_date,
                   b.modified_date,
                   b.market_country,
                   row_number() OVER (PARTITION BY trim(b.gtin_upc)
                                      ORDER BY f.publication_date DESC, b.modified_date DESC,
                                               try_cast(b.fdc_id AS BIGINT) DESC) AS rn
            FROM read_csv('{branded}', all_varchar = true, header = true) b
            JOIN read_csv('{food}',    all_varchar = true, header = true) f USING (fdc_id)
            WHERE trim(coalesce(b.gtin_upc, '')) <> ''
              AND b.market_country IN ('United States', 'US')
              AND trim(coalesce(b.discontinued_date, '')) = ''
          )
          WHERE rn = 1
          {order}
        ) TO '{out}' (FORMAT JSON)
    """)
    n_lines = sum(1 for _ in open(out, "rb"))
    counts = con.execute(f"""
        SELECT count(*) FILTER (WHERE market_country = 'United States'),
               count(*) FILTER (WHERE market_country = 'US')
        FROM read_json_auto('{out}', format = 'newline_delimited')
    """).fetchone()
    print(f"extract: wrote {n_lines:,} rows to {out} in {time.time() - t0:.1f}s "
          f"('United States' {counts[0]:,}; 'US' {counts[1]:,}; "
          f"{'SAMPLE ' + str(args.sample) if args.sample else 'FULL, ORDER BY gtin'})")


if __name__ == "__main__":
    main()
