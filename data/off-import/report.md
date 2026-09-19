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
| size parsed (value + unit) | 163,387 | 15.1% |
| quantity present but no size (multipack / unmappable unit / serving echo) | 66,229 | 6.1% |
| is_organic = true (`en:usda-organic` only) | 18,205 | 1.7% |
| categories present (proxy produced ≥ 1 category string) | 448,558 | 41.3% |
| **category mapped to a canonical name** | **287,607** | 26.5% of rows out · 64.1% of rows with categories |
| category_id resolved through the id map | 287,607 | 100.0% |

## 5. Mapped canonical categories

| canonical category | rows |
|---|---|
| Snacks & Chips | 106,188 |
| Beverages — Alcoholic | 47,452 |
| Condiments & Sauces | 39,940 |
| Frozen Foods | 24,276 |
| Breakfast & Cereal | 12,233 |
| Meat & Poultry | 11,833 |
| Bakery & Bread | 11,741 |
| Pasta, Rice & Grains | 10,087 |
| Seafood | 7,808 |
| Nuts & Dried Fruit | 5,674 |
| Soups & Broths | 5,639 |
| Dairy & Eggs | 2,704 |
| Coffee & Tea | 998 |
| Vitamins & Supplements | 495 |
| Cookies & Crackers | 202 |
| Produce | 121 |
| Deli & Prepared Foods | 105 |
| Oils & Vinegars | 35 |
| Baby & Toddler | 27 |
| Laundry | 13 |
| Health & Pharmacy | 10 |
| Baking Supplies | 9 |
| Canned & Jarred Goods | 3 |
| Floral & Plants | 3 |
| Household Cleaning | 3 |
| Paper & Plastic Goods | 2 |
| Personal Care & Hygiene | 2 |
| Pet Food & Supplies | 2 |
| Spices & Seasonings | 2 |

## 6. Top 30 unmapped `en:` category tags (rows with categories but no canonical match)

| tag | rows |
|---|---|
| en:plant-based-foods-and-beverages | 43,680 |
| en:plant-based-foods | 41,751 |
| en:undefined | 34,631 |
| en:dairies | 27,087 |
| en:fermented-foods | 21,142 |
| en:fermented-milk-products | 20,995 |
| en:fruits-and-vegetables-based-foods | 15,810 |
| en:cheeses | 14,199 |
| en:meals | 11,389 |
| en:cereals-and-potatoes | 9,628 |
| en:fats | 8,963 |
| en:cereals-and-their-products | 8,841 |
| en:vegetables-based-foods | 8,721 |
| en:desserts | 8,694 |
| en:dietary-supplements | 8,034 |
| en:canned-foods | 7,812 |
| en:canned-plant-based-foods | 7,733 |
| en:vegetable-fats | 7,333 |
| en:dairy-desserts | 6,726 |
| en:fermented-dairy-desserts | 6,575 |
| en:yogurts | 6,428 |
| en:salted-snacks | 5,711 |
| en:legumes-and-their-products | 5,671 |
| en:vegetable-oils | 5,580 |
| en:vegetables | 4,794 |
| en:spreads | 3,895 |
| en:sweeteners | 3,809 |
| en:milks | 3,784 |
| en:bodybuilding-supplements | 3,720 |
| en:fruits-based-foods | 3,551 |
