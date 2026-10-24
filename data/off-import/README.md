# Open Food Facts import — cleaning scripts

These are the scripts GroceryScouter uses to build its local copy of Open Food
Facts product-identity data (`ref_products_off`). The data is licensed under
the [Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/);
the app's public notice is <https://emanuels789.github.io/grocery-verify/data.html>.
This folder is published there as the "cleaning scripts" the licence asks us to
offer, together with each load's `report.md`.

**Republished 2026-10-24** (from commit `7d5c5b7b`, the PR #55 merge): the package-size
grammar these scripts import — `parseOffSize` in `supabase/functions/off-proxy/handler.ts`,
outside this folder — gained the count spellings (`pcs` / `pc` / `piece(s)` / `unit(s)` /
`ct` / `count` / `ea` / `each` / `stuk(s)` → the `ct` display unit, the weakest size), the
`fl.oz` spelling and the UN/CEFACT Rec. 20 unit codes (`ONZ` `LBR` `OZA` `LTR` `MLT` `GRM`
`GLL` `KGM` `QTL` `H87`) on 2026-10-24, so the cleaner published here (unchanged since the
2026-09-19 publish apart from `normalize.ts`'s header comment) now fills sizes the
September 15 load left empty; the loaded rows were brought up to date in place by
`size_count_updates.ts` (below), never a reload.

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

## size_updates.ts (2026-09-27 — SMALL CLEANUPS 1, DEV UPDATE queue)

Not part of the load. When the size grammar changed (the first slash-separated
token of the label now wins — `8 oz/227 g` → 8 oz), the loaded rows kept their
old sizes. `size_updates.ts` re-runs the cleaner's own row function over
`out/off_us.jsonl` and writes `out/size_updates.csv` (`barcode,size_value,size_unit`)
for the slash-label rows only; the queue in PROGRESS.md `\copy`s it into a
TEMPORARY table and joins it in one UPDATE. Never a reload (standing decision #12).

```
deno run --no-lock --config scripts/data-import/deno.json --allow-read=scripts/data-import/off/out --allow-write=scripts/data-import/off/out scripts/data-import/off/size_updates.ts
```

## category_updates.ts (2026-10-08 — PRODUCT TRIO, DEV RE-MAP queue)

Not part of the load. When the category mapper gained its second alias batch
(the Snacks & Chips misroute — chocolate, candy, biscuits and crackers filed
under Snacks because OFF lists `en:snacks` first — plus the NULL tags of the
2026-10-08 DEV coverage audit), the loaded rows kept the categories they were
loaded with. `category_updates.ts` re-runs the cleaner's own row function over
`out/off_us.jsonl` and writes `out/category_updates.csv` (`barcode,category_name`)
for every row that maps today — the canonical NAME, never a uuid, because the
category-id map is per environment (above). The queue in PROGRESS.md `\copy`s
it into a TEMPORARY table, joins `item_categories` by name and applies the
UPDATE in batches of 10,000 with a `pg_database_size` read-back between
batches (`IS DISTINCT FROM` skips rows whose category already matches). Rows
that map to nothing are not emitted — the UPDATE never writes a NULL. Never a
reload (standing decision #12).

```
deno run --no-lock --config scripts/data-import/deno.json --allow-read=scripts/data-import/off/out --allow-write=scripts/data-import/off/out scripts/data-import/off/category_updates.ts
```

## size_count_updates.ts (2026-10-24 — OFF SIZE GRAMMAR, COUNT SPELLINGS, DEV RE-MAP queue)

Not part of the load. When the size grammar learned the count spellings
(`pcs` / `pieces` / `unit(s)` / `ct` / `count` / `ea` / `each` / `pc` / `stuk(s)` →
the `ct` display unit) and the `fl.oz` spelling (→ `fl oz`), the loaded rows
kept the NULL sizes they were loaded with — and, the same day, the UN/CEFACT
Rec. 20 unit codes the USDA release writes (`ONZ` `LBR` `OZA` `LTR` `MLT` `GRM`
`GLL` `KGM` `QTL` `H87`; `CLT` excluded — the app has no `cl` unit).
`size_count_updates.ts` is ONE
script for BOTH reference tables — `--source off` re-runs this cleaner's row
function over `out/off_us.jsonl`, `--source usda` the USDA cleaner's over
`../usda/out/usda_us.jsonl` — and writes `size_count_updates.csv`
(`barcode,size_value,size_unit`) beside the extract it read, for the rows whose
new size the old grammar could not have produced (a count, or a first token
spelling `fl.oz` or one of the Rec. 20 codes). Two passes: the first records the newest copy per canonical
barcode over EVERY gated row (as the load did), the second emits the survivor.
The queue in PROGRESS.md `\copy`s each CSV into a TEMPORARY table and applies
the UPDATE in batches of 10,000 (`IS DISTINCT FROM` is the diff; nothing that
fails to parse is emitted, so the UPDATE never writes a NULL). Never a reload
(standing decision #12); there is no PROD half — PROD's tables are empty until
launch day and that load runs this grammar.

```
deno run --no-lock --config scripts/data-import/deno.json --allow-read=scripts/data-import --allow-write=scripts/data-import scripts/data-import/off/size_count_updates.ts --source off
deno run --no-lock --config scripts/data-import/deno.json --allow-read=scripts/data-import --allow-write=scripts/data-import scripts/data-import/off/size_count_updates.ts --source usda
```
