# USDA FoodData Central import — cleaning scripts

These are the scripts GroceryScouter uses to build its local copy of USDA
FoodData Central's **Branded Foods** product-identity data (the Global Branded
Food Products Database) — the table `ref_products_usda`, read after the Open
Food Facts table and before any live lookup. The data is in the public domain
under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/); no
attribution is required, and the app shows a one-line courtesy notice. This
folder is published on the app's data page beside each load's `report.md`,
the same way the Open Food Facts scripts are.

Nothing here talks to the network. The CSV download, the database load and
the swap are run by hand, outside these scripts.

## What is kept, what is dropped

Kept per product: the barcode (in the app's canonical form — a UPC-A is 12
digits), USDA's own spelling of the code, the product name (`description`,
title-cased when the source is all-caps — the only casing change), the brand
(`brand_name` unless empty or `N/A`, else `brand_owner`), the brand owner, the
package size (value + unit) when the FIRST token of `package_weight` parses
cleanly ("16 oz/454 g" → 16 oz), the raw `branded_food_category` and our mapped
category, the raw `package_weight`, the `fdc_id`, the publication date, and a
store-brand flag.

Dropped: ingredients, nutrition, serving sizes, every row outside the US market
or marked discontinued, every 14-digit code with a non-zero packaging indicator
(a case code, not a retail identity), every row that fails the same barcode
gate a scanned label must pass (wrong length or check digit), and every row
without a name.

## The three committed inputs beside the scripts

- **`usda_category_map.csv`** — `branded_food_category` → one of the app's 34
  canonical categories (or blank). Hand-reviewed; any non-empty `canonical`
  counts, whatever the `status` column says.
- **`usda_retailer_owners.csv`** — retailer strings matched inside
  `brand_owner` to set `is_store_brand` (OR'd with the app's own store-brand
  list on the name and brand). Retailers only — no wholesaler cooperatives.
- the target environment's **`item_categories.csv`** — see the warning below.

## Run order

```
# 0. one-time, outside the repo
pip install duckdb
#    download the FoodData Central "Branded" CSV release (branded_food.csv + food.csv)

# 1. CSV release → JSONL (DuckDB: join, one row per raw spelling, US + not discontinued, column projection)
python scripts/data-import/usda/extract.py <release-folder> --sample 1000   # dry run
python scripts/data-import/usda/extract.py <release-folder>                 # full

# 2. the TARGET ENVIRONMENT'S category ids — see the warning below
#    psql (connected to THAT database): \copy (select id, name from public.item_categories order by name) to 'scripts/data-import/usda/out/item_categories.csv' csv header

# 3. JSONL → load CSV + report.md (Deno; the app's own normalisers)
deno run --config scripts/data-import/deno.json \
  --allow-read=scripts/data-import/usda --allow-write=scripts/data-import/usda/out \
  scripts/data-import/usda/clean.ts

# 4. load (psql, run by hand): staging, then the atomic swap, then the read-backs
#    \copy public.ref_products_usda_staging (barcode,gtin_upc_raw,name,brand,brand_owner,size_value,size_unit,category_id,usda_category,package_weight_raw,fdc_id,usda_publication_date,is_store_brand) from 'scripts/data-import/usda/out/load.csv' csv header
#    select public.swap_ref_products_usda();
#    select count(*) from public.ref_products_usda where length(barcode) = 14;   -- MUST be 0
```

`out/` is ignored by git — it holds the data, never source.

## ⚠️ The category-id map is PER ENVIRONMENT

`item_categories.id` is `gen_random_uuid()` in every database, so the DEV
export is **not** the PROD export. A `load.csv` cleaned with the DEV map
carries DEV uuids in `category_id`; loading it into PROD would violate the
foreign key (the `\copy` fails) — or, worse, would silently point at wrong
categories if a uuid happened to exist. **Export the map from the database
you are about to load, re-run step 3 against it, and only then `\copy`.**
One clean per target; never reuse a `load.csv` across databases.

## ⚠️ Zero 14-digit rows — the read-back is the enforcement

USDA spells many UPC-As as 14 digits with two leading zeros. The cleaner
strips the packaging indicator (`0`) and lets the app's gate fold the rest to
the 12-digit form the app stores. The database's barcode CHECK would accept a
14-digit string, so a cleaner bug here would load silently: the report prints
the 14-digit count (must be 0) and step 4's last query asserts it on the
loaded table.

## Rows that never enter the table

Besides case codes, gate failures and nameless rows, every
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
