# OFF cleaner report

Input: `off_us.jsonl` (1,315,018 rows; pass-1 count 1,315,018); categories map: 34 canonical names → ids. Output: `load.csv` for `\copy public.ref_products_off_staging (barcode,off_code,name,brand,size_value,size_unit,category_id,categories_tags,is_organic,labels_tags,off_quantity,off_last_modified,origin) FROM … CSV HEADER`.

Every decision below is the app's own code: `normalizeScannedBarcode` (gate + canonical form), the proxy's `mapOffProduct` (name / brand / categories / size / organic — identical to a live answer), `mapOffCategoriesToCanonical` (category). Deterministic — same inputs, same bytes.

## 1. Rows

| metric | rows | % of input |
|---|---|---|
| rows in | 1,315,018 | 100% |
| **rows out (loadable)** | **1,085,050** | 82.5% |
| rejected | 229,960 | 17.5% |
| duplicate copies dropped (newest `last_modified_t` kept) | 8 | 0.0% |
| of rows out: tagged `en:united-states` | 825,706 | 76.1% |
| of rows out: UPC-A-shaped but not US-tagged (filter (c)) | 259,344 | 23.9% |

## 2. Rejects by reason

| reason | rows | what it means |
|---|---|---|
| bad_barcode | 58,609 | fails the scanned-payload gate: not 8 / 12 / 13 / 14 digits after the fold, or a bad GS1 check digit (zero-padded PLUs, OFF-internal ids, misreads) |
| restricted_circulation | 155,862 | a number-system-2 code (GS1 prefix 20–29): store-assigned, variable-weight / in-store — never globally unique, so never a reference row (standing rule, 2026-09-16) |
| empty_name | 15,489 | no `main` and no `en` product name — nothing to show a user |
| name_too_long | 0 | name over the 500-char DB CHECK |
| malformed_row | 0 | unparseable JSON line or missing code / timestamp |

Size is never a reject: the proxy omits a size it cannot parse (or that merely echoes the serving size) and the user types it — the same row is still an identity. See §4.

## 3. Canonical barcode form of rows out

| length | rows | form |
|---|---|---|
| 8 | 30,716 | EAN-8 / UPC-E |
| 12 | 961,181 | UPC-A (the dump's 13-digit `0…` folded) |
| 13 | 92,556 | EAN-13 |
| 14 | 597 | GTIN-14 |

## 4. Field fill on rows out

| field | rows | % of rows out |
|---|---|---|
| brand | 725,000 | 66.8% |
| brand dropped (over 500 chars) | 0 | 0.0% |
| size parsed (value + unit) | 169,679 | 15.6% |
| quantity present but no size (multipack / unmappable unit / serving echo) | 59,937 | 5.5% |
| is_organic = true (`en:usda-organic` only) | 18,205 | 1.7% |
| categories present (proxy produced ≥ 1 category string) | 448,558 | 41.3% |
| **category mapped to a canonical name** | **395,756** | 36.5% of rows out · 88.2% of rows with categories |
| category_id resolved through the id map | 395,756 | 100.0% |

## 5. Mapped canonical categories

| canonical category | rows |
|---|---|
| Snacks & Chips | 53,641 |
| Beverages — Non-Alcoholic | 44,282 |
| Condiments & Sauces | 41,020 |
| Dairy & Eggs | 29,791 |
| Candy & Chocolate | 29,669 |
| Frozen Foods | 24,276 |
| Pasta, Rice & Grains | 22,009 |
| Bakery & Bread | 21,591 |
| Cookies & Crackers | 19,067 |
| Deli & Prepared Foods | 15,554 |
| Meat & Poultry | 12,632 |
| Breakfast & Cereal | 12,192 |
| Produce | 9,325 |
| Oils & Vinegars | 8,592 |
| Baking Supplies | 8,542 |
| Vitamins & Supplements | 8,485 |
| Canned & Jarred Goods | 7,813 |
| Seafood | 7,808 |
| Nuts & Dried Fruit | 7,077 |
| Soups & Broths | 5,639 |
| Beverages — Alcoholic | 3,170 |
| Coffee & Tea | 2,394 |
| Baby & Toddler | 1,150 |
| Laundry | 13 |
| Health & Pharmacy | 10 |
| Floral & Plants | 3 |
| Household Cleaning | 3 |
| Paper & Plastic Goods | 2 |
| Personal Care & Hygiene | 2 |
| Pet Food & Supplies | 2 |
| Spices & Seasonings | 2 |

## 6. Top 30 unmapped `en:` category tags (rows with categories but no canonical match)

| tag | rows |
|---|---|
| en:undefined | 34,507 |
| en:plant-based-foods-and-beverages | 2,103 |
| en:plant-based-foods | 1,754 |
| en:meats-and-their-products | 1,388 |
| en:null | 1,161 |
| en:prepared-meats | 888 |
| en:meat-alternatives | 577 |
| en:beverages-and-beverages-preparations | 435 |
| en:beverage-preparations | 429 |
| en:meat-analogues | 427 |
| en:cured-sausages | 389 |
| en:meat-preparations | 360 |
| en:food-additives | 341 |
| en:chicken-and-its-products | 304 |
| en:breaded-products | 300 |
| en:salami | 267 |
| en:dairy-substitutes | 265 |
| en:chicken-preparations | 256 |
| en:cocoa-and-its-products | 242 |
| en:breaded-chicken | 238 |
| en:stuffing | 230 |
| en:cocoa-and-chocolate-powders | 220 |
| en:protein-supplement | 209 |
| en:sweet-pies | 186 |
| en:hams | 185 |
| en:poultry-nuggets | 185 |
| en:flours | 184 |
| en:chicken-nuggets | 182 |
| en:groceries | 175 |
| en:cocoa-powders | 153 |
