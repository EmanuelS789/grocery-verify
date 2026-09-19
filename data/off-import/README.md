# Open Food Facts import — cleaning scripts

These are the scripts GroceryScouter uses to build its local copy of Open Food
Facts product-identity data (`ref_products_off`). The data is licensed under
the [Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/);
the app's public notice is <https://emanuels789.github.io/grocery-verify/data.html>.
This folder is published there as the "cleaning scripts" the licence asks us to
offer, together with each load's `report.md`.

Nothing here talks to the network. The Parquet download, the database load and
the swap are run by hand, outside these scripts.

## What is kept, what is dropped

Kept per product: the barcode (in the app's canonical form — a UPC-A is 12
digits), Open Food Facts' own spelling of the code, product name, first brand,
package size (value + unit) when it parses cleanly, the raw `categories_tags`
and our mapped category, the raw `labels_tags` and whether `en:usda-organic` is
present, the raw quantity label, and `last_modified_t`.

Dropped: nutrition, ingredients, images, serving size (used only to suppress a
size that merely echoes it), and every row that fails the same barcode gate a
scanned label must pass (wrong length or check digit), or has no product name.

Rows are filtered to `en:united-states` plus every row whose code is a valid
UPC-A shape (GS1-US/Canada numbers) — the country tag alone misses real US
products.

## Run order

```
# 0. one-time, outside the repo
pip install duckdb
#    download food.parquet from huggingface.co/datasets/openfoodfacts/product-database

# 1. Parquet → JSONL (DuckDB: row filter + column projection only)
python scripts/data-import/off/extract.py <path/to/food.parquet> --sample 1000   # dry run
python scripts/data-import/off/extract.py <path/to/food.parquet>                 # full

# 2. the TARGET ENVIRONMENT'S category ids — see the warning below
#    psql (connected to THAT database): \copy (select id, name from public.item_categories order by name) to 'scripts/data-import/off/out/item_categories.csv' csv header

# 3. JSONL → load CSV + report.md (Deno; the app's own normalisers)
deno run --config scripts/data-import/deno.json \
  --allow-read=scripts/data-import/off/out --allow-write=scripts/data-import/off/out \
  scripts/data-import/off/clean.ts

# 4. load (psql, run by hand): staging, then the atomic swap
#    \copy public.ref_products_off_staging (barcode,off_code,name,brand,size_value,size_unit,category_id,categories_tags,is_organic,labels_tags,off_quantity,off_last_modified,origin) from 'scripts/data-import/off/out/load.csv' csv header
#    select public.swap_ref_products_off();
```

`out/` is ignored by git — it holds ODbL data, never source.

## ⚠️ The category-id map is PER ENVIRONMENT

`item_categories.id` is `gen_random_uuid()` in every database, so the DEV
export is **not** the PROD export. A `load.csv` cleaned with the DEV map
carries DEV uuids in `category_id`; loading it into PROD would violate the
foreign key (the `\copy` fails) — or, worse, would silently point at wrong
categories if a uuid happened to exist. **Export the map from the database
you are about to load, re-run step 3 against it, and only then `\copy`.**
One clean per target; never reuse a `load.csv` across databases.

## Rows that never enter the table

Besides rows that fail the barcode gate or have no name, every
**number-system-2 code** (GS1 prefix 20–29 — store-assigned variable-weight
and in-store codes) is rejected: those codes are not globally unique, so a
reference row for one would be wrong for every store but the one that
printed it. This is a standing rule for every reference table, from every
source.

## Tests

```
deno test --config scripts/data-import/deno.json scripts/data-import/
```

(also the second half of `npm run test:deno`). The tests use bracketed
placeholder names, never real products.
