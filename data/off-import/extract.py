#!/usr/bin/env python3
"""
scripts/data-import/off/extract.py — STEP 1 of the OFF import: Parquet → JSONL

PURPOSE: Pull the rows and columns the TypeScript cleaner (clean.ts) needs
         out of Open Food Facts' `food.parquet` (ODbL) into a newline-
         delimited JSON file under scripts/data-import/off/out/ (gitignored).
         DuckDB does ONLY what SQL is good at — the row filter and the column
         projection; every normalisation decision (barcode gate, canonical
         form, name/brand/size/organic mapping, category mapping, dedupe)
         is made by clean.ts with the app's own code, never here.

WHY JSONL, NOT CSV (gate-1 pick 0, 2026-09-15): the list columns
         (categories_tags, labels_tags) survive as real JSON arrays and the
         cleaner streams the file with JSON.parse per line — no CSV quoting
         rules, no extra dependency.

THE ROW FILTER (gate-1 pick 1, "barcode-shape (c)"): a row is kept when it
         is tagged `en:united-states` OR when it looks like a valid UPC-A
         with a name — a 13-digit `0…` code whose GS1 mod-10 check passes.
         A UPC-A is a GS1-US/Canada number by construction; the community's
         country tag is the least reliable field in the row (one PROD
         `source='off'` product is tagged France). The mod-10 here is the
         same SQL twin probe.py self-tests against the Python port of
         barcodeClassifier.ts; it only widens the filter — clean.ts's gate is
         the one that decides acceptance.

DETERMINISM: the full extract is ORDER BY code; --sample N takes N rows in
         hash(code) order (a fixed, reproducible pseudo-random sample — the
         §3 dry run uses 1,000).

INPUTS / OUTPUTS (all outside git):
  argv[1]        path to food.parquet (e.g. ~/Documents/OFFData/food.parquet)
  --out <path>   JSONL to write (default scripts/data-import/off/out/off_us.jsonl)
  --sample N     write only N rows (deterministic sample) — the dry run

RUN:
  python scripts/data-import/off/extract.py <parquet> --sample 1000
  python scripts/data-import/off/extract.py <parquet>              # full

NOT a test, NOT a build step, NOT in `npm run test:all`.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import US_TAG, self_test, sql_gs1_ok  # noqa: E402  (same folder)

sys.stdout.reconfigure(encoding="utf-8")

SAMPLE_SEED = "20260916"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "off_us.jsonl")

# The columns clean.ts reads — nothing else leaves the Parquet.
PROJECTION = """
    code,
    list_filter(product_name, lambda x: x.lang = 'main')[1].text AS name_main,
    list_filter(product_name, lambda x: x.lang = 'en')[1].text   AS name_en,
    brands,
    categories,
    categories_tags,
    quantity,
    product_quantity,
    product_quantity_unit,
    serving_size,
    labels_tags,
    last_modified_t,
    coalesce(list_contains(countries_tags, '{us}'), false)     AS is_us
""".format(us=US_TAG)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--sample", type=int, default=None)
    args = ap.parse_args()

    path = args.parquet.replace("\\", "/")
    if not os.path.isfile(path):
        raise SystemExit(f"not a file: {path}")
    out = args.out.replace("\\", "/")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    self_test(con)

    upc_a_shaped = (
        "(len(code) = 13 AND code[1] = '0' AND " + sql_gs1_ok("code")
        + " AND list_filter(product_name, lambda x: x.lang = 'main')[1].text IS NOT NULL)"
    )
    where = f"(list_contains(countries_tags, '{US_TAG}') OR {upc_a_shaped})"
    order = f"ORDER BY hash(code || '{SAMPLE_SEED}') LIMIT {int(args.sample)}" if args.sample else "ORDER BY code"

    t0 = time.time()
    con.execute(f"""
        COPY (
            SELECT {PROJECTION}
            FROM read_parquet('{path}')
            WHERE {where}
            {order}
        ) TO '{out}' (FORMAT JSON)
    """)
    # DuckDB's JSON COPY writes one object per line (ARRAY false by default).
    n_lines = sum(1 for _ in open(out, "rb"))
    counts = con.execute(f"""
        SELECT count(*) FILTER (WHERE is_us) AS us_tagged,
               count(*) FILTER (WHERE NOT is_us) AS upc_a_only
        FROM read_json_auto('{out}', format = 'newline_delimited')
    """).fetchone()
    print(f"extract: wrote {n_lines:,} rows to {out} in {time.time() - t0:.1f}s "
          f"(US-tagged {counts[0]:,}; UPC-A-shaped but not US-tagged {counts[1]:,}; "
          f"{'SAMPLE ' + str(args.sample) if args.sample else 'FULL, ORDER BY code'})")


if __name__ == "__main__":
    main()
