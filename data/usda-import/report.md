# USDA cleaner report

Input: `usda_us.jsonl` (463,923 rows; pass-1 count 463,923); category map: 245 USDA categories → a canonical name; id map: 34 canonical names → ids; owner list: 58 retailer strings. Output: `load.csv` for `\copy public.ref_products_usda_staging (barcode,gtin_upc_raw,name,brand,brand_owner,size_value,size_unit,category_id,usda_category,package_weight_raw,fdc_id,usda_publication_date,is_store_brand) FROM … CSV HEADER`.

Every decision below is the app's own code or a committed, reviewed input: the indicator-0 pre-fold then `normalizeScannedBarcode` (gate + canonical form), `hasRestrictedCirculationPrefix` (prefix-2), the proxy's `mapOffProduct` size grammar on the FIRST slash token of `package_weight`, `classifyStoreBrand` OR `usda_retailer_owners.csv` (store brand), `usda_category_map.csv` (category). Names: title-cased only when the source is all-caps; brand = `brand_name` unless empty / `N/A`, else `brand_owner`. Deterministic — same inputs, same bytes.

## 1. Rows

| metric | rows | % of input |
|---|---|---|
| rows in | 463,923 | 100% |
| **rows out (loadable)** | **425,222** | 91.7% |
| rejected | 16,037 | 3.5% |
| duplicate spellings dropped (newest `publication_date` per canonical barcode kept) | 22,664 | 4.9% |
| of rows out: `market_country = 'United States'` (the rest spelled `US`) | 418,504 | 98.4% |
| of rows out: pre-folded from a 14-digit indicator-0 spelling | 23,594 | 5.5% |
| of rows out: raw spelling carried whitespace (stripped; `gtin_upc_raw` stores the digits) | 5 | 0.0% |

## 2. Rejects by reason

| reason | rows | what it means |
|---|---|---|
| case_code | 3,456 | 14 digits with a NON-ZERO packaging indicator — a case / logistics code, never a retail identity |
| bad_barcode | 10,872 | fails the scanned-payload gate after the pre-fold: not 8 / 12 / 13 / 14 digits, or a bad GS1 check digit (11-digit exports, misreads, non-digits) |
| restricted_circulation | 1,704 | a number-system-2 code (GS1 prefix 20–29): store-assigned, variable-weight / in-store — never globally unique, so never a reference row (standing decision #11) |
| empty_name | 0 | no `description` — nothing to show a user |
| name_too_long | 5 | name over the 500-char DB CHECK |
| malformed_row | 0 | unparseable JSON line, or a missing / non-positive fdc_id or a bad publication_date |

Size is never a reject: an unparsed label leaves the size pair NULL and the user types it — the row is still an identity.

## 3. Canonical barcode form of rows out

| length | rows | form |
|---|---|---|
| 8 | 2,080 | EAN-8 / UPC-E |
| 12 | 417,250 | UPC-A (12 as written, or the `0…`13 / `00…`14 spellings folded) |
| 13 | 5,892 | EAN-13 (non-zero lead; incl. a `0`-indicator GTIN-14 body) |

**14-digit rows out: 0** — as required (the DB CHECK would accept them; the cleaner is the enforcement).

## 4. Field fill on rows out

| field | rows | % of rows out |
|---|---|---|
| brand (after the `brand_name` → `brand_owner` rule) | 424,261 | 99.8% |
| …brand taken from `brand_owner` (brand_name empty or a placeholder) | 17,693 | 4.2% |
| …brand_name was a placeholder literal (`N/A` / `NA`) | 350 | 0.1% |
| brand / owner dropped (over 500 chars) | 0 | 0.0% |
| name title-cased (source was all-caps) | 406,614 | 95.6% |
| size parsed (first `package_weight` token through the app grammar) | 358,422 | 84.3% |
| package_weight present but no size (spelling noise: `ONZ`, `0Z`, `LBR`, fractions, multipacks) | 17,103 | 4.0% |
| usda_category present | 425,069 | 100.0% |
| **category mapped to a canonical name (map hit)** | **425,067** | 100.0% of rows out · 100.0% of rows with a category |
| category_id resolved through the id map | 425,067 | 100.0% |
| **is_store_brand = true** | **88,224** | 20.7% |
| …by the app's `classifyStoreBrand` (name + brand) | 42,733 | 10.0% |
| …by the owner list (`brand_owner` contains a retailer string) | 83,238 | 19.6% |
| …by the owner list ONLY (the classifier gap the flag closes) | 45,491 | 10.7% |

Units produced by the size parse: `oz` 275,164, `fl oz` 50,157, `lb` 8,839, `g` 8,751, `qt` 4,156, `gal` 3,322, `pt` 2,986, `ml` 2,154, `L` 1,751, `kg` 1,142.

## 5. Mapped canonical categories

| canonical category | rows |
|---|---|
| Snacks & Chips | 51,597 |
| Beverages — Non-Alcoholic | 36,470 |
| Frozen Foods | 36,009 |
| Dairy & Eggs | 35,728 |
| Candy & Chocolate | 34,507 |
| Condiments & Sauces | 31,969 |
| Canned & Jarred Goods | 30,591 |
| Bakery & Bread | 25,186 |
| Cookies & Crackers | 22,877 |
| Deli & Prepared Foods | 20,888 |
| Breakfast & Cereal | 19,797 |
| Pasta, Rice & Grains | 11,614 |
| Spices & Seasonings | 11,428 |
| Meat & Poultry | 11,095 |
| Baking Supplies | 10,254 |
| Produce | 8,345 |
| Seafood | 7,661 |
| Soups & Broths | 4,829 |
| Oils & Vinegars | 4,366 |
| Coffee & Tea | 4,255 |
| Nuts & Dried Fruit | 3,755 |
| Beverages — Alcoholic | 1,105 |
| Vitamins & Supplements | 535 |
| Baby & Toddler | 150 |
| Health & Pharmacy | 51 |
| Tobacco & Vaping | 3 |
| Personal Care & Hygiene | 2 |

## 6. Top 30 unmapped USDA categories (rows whose category is NOT IN THE MAP AT ALL — a vocabulary the map has never seen)

| usda_category | rows |
|---|---|

Categories the map lists with a blank canonical ON PURPOSE (a recorded decision, not a gap; the rows load with `category_id` NULL): Gardening 1, Media 1.

## 7. Owner-list hits (rows flagged store-brand through `brand_owner`; a row can hit more than one string)

| owner string | rows |
|---|---|
| target stores | 9,133 |
| wal-mart | 8,957 |
| meijer | 5,834 |
| kroger | 5,743 |
| safeway | 5,140 |
| ahold | 4,188 |
| hy-vee | 4,050 |
| wakefern | 3,571 |
| whole foods market | 3,070 |
| weis markets | 2,845 |
| giant eagle | 2,640 |
| wegmans | 2,435 |
| schnuck | 2,381 |
| smart & final | 2,263 |
| hannaford | 2,031 |
| publix | 1,889 |
| big y | 1,756 |
| raley's | 1,674 |
| stater bros | 1,591 |
| tops markets | 1,551 |
| harris-teeter | 1,472 |
| walgreen | 1,230 |
| brookshire | 1,139 |
| cvs pharmacy | 985 |
| price chopper | 861 |
| bj's wholesale | 766 |
| save mart | 637 |
| lunds | 570 |
| fareway | 492 |
| market basket | 426 |
| winco | 357 |
| ingles markets | 353 |
| sam's club | 309 |
| the fresh market | 306 |
| piggly wiggly | 223 |
| costco | 139 |
| trader joe | 72 |
| shoprite | 60 |
| amazon fulfillment services | 30 |
| harris teeter | 18 |
| fred meyer | 15 |
| sprouts farmers | 8 |
| giant food | 6 |
| lidl | 6 |
| heinen's | 5 |
| albertsons | 4 |
| aldi inc | 3 |
| target corporation | 3 |
| dollar general | 1 |
