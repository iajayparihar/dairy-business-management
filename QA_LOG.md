# QA log — module by module

Goal: the app will run for years, for many dairy shops across India (many tenants, slow mobile
networks, retries, double taps, Hindi names, shared phones). Each module: read the code, write a
QA sweep (`tests/<area>/test_*_qa.py`), list bugs here, fix, run only that module's tests.

Order: customers → suppliers & milk rates → products & prices → milk collection → purchases →
supplier payments → routes & deliveries → shop sales → customer bills → customer payments →
stock & production → expenses → employees & salary → reports & dashboards.

Status key: FIXED, OPEN (needs owner decision), NOTE (carry to another module).

## Cross-cutting patterns (check in every module)

- Plain list views have no pagination (the project rule says lists use `StandardPagination`).
- `IntegrityError` on a unique constraint becomes a 500: catch it and return 400.
- Unknown request fields are silently dropped: use `RejectUnknownFieldsMixin` (apps/common/serializers.py).
- Codes that end up in document numbers must be `A-Z 0-9 _ -`, upper-cased (`clean_code`).
- Phones: stored as digits with optional `+` (`clean_phone`); searches strip spaces and dashes the same way.
- Order lists by a unique tie-break so pages do not skip or repeat rows.
- Soft delete must record who and when (`soft_delete(user)`), not call `delete()`.

## 1. Customers (`apps/customers`) — tests: `tests/customer/test_customer_qa.py`

| # | Bug | Severity | Status |
|---|---|---|---|
| C1 | Creating a customer with an existing code returned **500** (unique constraint, `business` is read-only so DRF cannot validate it). Same for a code of a soft-deleted customer and for PATCH to an existing code. | High | FIXED |
| C2 | Parallel creates of the same code could 500. Now 400, exactly one wins. | Medium | FIXED |
| C3 | Code accepted spaces, symbols, Hindi, newline, lower/upper variants (`abc` and `ABC` both allowed). Codes go into bill / payment numbers. Now `A-Z 0-9 _ -`, upper-cased, 1-30 chars. | Medium | FIXED |
| C4 | Code could be renamed after bills / payments existed; a new customer could then take the old code and collide with old bill numbers. Rename is blocked once the customer has records. | High | FIXED |
| C5 | Phone accepted anything (`abc`, `<script>`). Now 7-15 digits with optional `+`; spaces / dashes / brackets removed on save. | Medium | FIXED |
| C6 | Customer list was **not paginated** (a shop with thousands of customers got one huge response). Now `StandardPagination` (`{count, results}`); the counter's `?search=` keeps its documented bare list of up to 20. | High | FIXED |
| C7 | List order `name` only: customers with equal names were skipped or repeated between pages. Tie-break on `customer_code`. | Medium | FIXED |
| C8 | Phone search failed when typed with `+91` / spaces. Now matched on the stored digits. | Medium | FIXED |
| C9 | No `is_active` / `customer_type` filters, so apps could not list only active customers. Added (bad values are 400). | Low | FIXED |
| C10 | Unknown fields (typos like `phon`) were silently dropped. Now 400. | Medium | FIXED |
| C11 | Soft delete never recorded who deleted (`deleted_by` stayed empty). | Low | FIXED |
| C12 | `quick-customer` (counter shortcut in sales) accepted any phone text. Now uses the same phone rules. | Low | FIXED |
| N1 | `quick-customer` generates `CUST-<n>` by scanning every customer code (slow with many customers) and creates a duplicate walk-in on every retry with the same phone. Review in the **shop sales** pass. | Medium | NOTE |
| N2 | Existing rows saved before this pass may hold phones with spaces; they only match searches typed the same way. No migration written (data is development data). | Low | NOTE |
| O1 | Two customers can share a phone (shops share numbers). Allowed on purpose; no rule invented. | — | OPEN (ask client if wanted) |

## 2. Suppliers & milk rates (`apps/suppliers`) — tests: `tests/supplier/test_supplier_qa.py`

| # | Bug | Severity | Status |
|---|---|---|---|
| S1 | Supplier create / patch with an existing code (also a soft-deleted one) returned **500**; parallel creates too. Now 400. | High | FIXED |
| S2 | Code format / case free-for-all although the code is part of every milk can number (`<SUPPLIER_CODE>-<YYMMDD>-...`). Now `A-Z 0-9 _ -`, upper-cased. | Medium | FIXED |
| S3 | Code could be renamed after milk / purchases / payments existed, so a new supplier could take the old code and collide with old can numbers. Frozen once there are records (`apps/suppliers/guards.py`). | High | FIXED |
| S4 | A supplier with milk, purchases or payments could be **deleted** (soft) with no guard, hiding money owed. Now 400, "mark inactive instead". Records who deleted. | High | FIXED |
| S5 | Supplier list not paginated, unstable order, no filters / search. Now paginated, ordered by name + code, `is_active`, `supplier_type`, `search` (name, code, phone). | Medium | FIXED |
| S6 | Phone free text; unknown fields silently dropped. Same fixes as customers. | Medium | FIXED |
| **R1** | **A supplier's milk rate could never be changed.** The new rate overlapped the open-ended old one (DB exclusion constraint) and there is no endpoint to close the old one; only rates planned with an end date worked. Now the same pattern as product prices: the running period ends where the new one starts, and the new one ends at the next scheduled rate; the supplier row is locked, and a duplicate start time is a 400. | **Critical** | FIXED |
| R2 | A timestamp without an offset was silently read in the server time zone (the "must be timezone-aware" check could never fire). Now 400 (`StrictDateTimeField`). | Medium | FIXED |
| R3 | Rate history list unpaginated; unknown request fields ignored. | Low | FIXED |
| N3 | `StrictDateTimeField` should also be used wherever clients send timestamps: payments, purchases / milk collection, deliveries, products serializers use plain `DateTimeField`. Check in each module. | Medium | NOTE |
| O2 | A rate can be set with `effective_from` in the past. Old collections keep their snapshot, but a *correction* re-resolves at the original time, so a backdated rate changes what corrections pay. Products forbid backdating; milk rates do not. Decide: forbid, or allow for the owner only. | Medium | OPEN |

## 3. Products, categories & prices (`apps/products`) — tests: `tests/products/test_product_qa.py`

| # | Bug | Severity | Status |
|---|---|---|---|
| P1 | Duplicate SKU or category name (also of a soft-deleted row) returned **500**; parallel creates too. Now 400. Category names compare case-insensitively. | High | FIXED |
| P2 | A product with sales, stock, purchases, deliveries or production could be **deleted** (soft), and a category with products too. Now 400 (guard scans every table that points at the product, so a new table is covered automatically). Records who deleted. | High | FIXED |
| P3 | `unit` and `track_stock` could be flipped after stock rows existed, corrupting balances; SKU could be renamed. Frozen once the product has records. | High | FIXED |
| P4 | Categories had **no update or delete endpoint** at all. Added `categories/<id>/`. | Medium | FIXED |
| P5 | Product, category lists unpaginated, unstable order, no filters. Now paginated; products filter by `is_active`, `track_stock`, `unit`, `category`, `search` (name, SKU). | Medium | FIXED |
| P6 | Setting a price "from now" with a phone clock a few seconds behind the server failed with "cannot be backdated". Up to 5 minutes early is read as now; older stays a 400. | Medium | FIXED |
| P7 | The price change locked the product row with a plain `FOR UPDATE`, which blocks inserts of deliveries / stock rows that reference the product while it runs. Now `no_key=True`. | Medium | FIXED |
| P8 | Timestamp without an offset was guessed in the server zone. Now 400. Unknown fields rejected on all three serializers. | Medium | FIXED |

## 4. Milk collection (`apps/purchases`, milk part) — tests: `tests/purchases/test_milk_api_qa.py`

Already well covered (100 milk tests pass). The sweep found:

| # | Bug | Severity | Status |
|---|---|---|---|
| M1 | `collected_at` could be **any future time** (a wrong phone date paid the supplier for milk not yet collected). Now 400, with 5 minutes of clock skew allowed (same rule as expenses). | High | FIXED |
| M2 | Unknown request fields (`amount`, `applied_rate`, `status`, typos) silently dropped on create / cancel / correct. Now 400. | Low | FIXED |
| N4 | A COW can reads back `fat: "0.00"` although fat is "not recorded" for cow; a UI may show "fat 0". Left as is (changing the stored column is a migration). | Low | NOTE |
| O3 | Fat accepts up to 999.99. A slip such as 45 instead of 4.5 multiplies the amount by 10. Real buffalo / cow fat is 3-12. Decide a sanity cap (for example 20) or a confirm step in the app. | Medium | OPEN |
| — | Verified OK: idempotent `client_ref` (409 on a changed replay), unique can numbers under 8 parallel requests, cancel vs correct race, shift boundaries at 15:00 IST (also from UTC), tenant isolation, roles, bad input never 500. | | |

## 5. Supplier purchases, returns & corrections (`apps/purchases`) — tests: `tests/purchases/test_purchase_api_qa.py`

| # | Bug | Severity | Status |
|---|---|---|---|
| U1 | A **retried** return / correction with the same `client_ref` was refused ("already submitted") instead of returning the first result, so after a lost response the user saw an error although the return was booked and might enter it again. Now an identical retry returns the original (200); a different request with the same ref is a 400. | High | FIXED |
| U2 | Purchase cancel accepted an empty reason (audit rule: reason, who, when). Now required. | Medium | FIXED |
| U3 | Purchase dated in the future accepted (create and patch). Now 400 (5 min skew). | Medium | FIXED |
| U4 | Adjustment history returned `net_amount`, `original_total` and line quantities / amounts as **floats** (`240.0`), because DRF encodes raw `Decimal` as float. Money is now a string everywhere in this API. | Medium | FIXED |
| U5 | Unknown fields accepted on every purchase / return / correction serializer; a purchase could carry unlimited lines (now max 200). | Low | FIXED |
| N5 | The milk `correct` response contains `"stock_shortfall": 0.0` (float) for the same reason. Check every raw `Response({...Decimal...})` in later modules (reports, stock, billing). | Low | NOTE |
| — | Verified OK: receive posts stock once even with 6 parallel receives; parallel returns cannot exceed the quantity; duplicate purchase numbers are 400 under parallel load; tenant isolation; staff blocked from cost data. | | |

## 6. Supplier payments (`apps/payments`) — tests: `tests/payment/test_payment_api_qa.py`

| # | Bug | Severity | Status |
|---|---|---|---|
| Y1 | A payment could be **dated in the future** (hides it from today's statement and cash-in-hand). Now 400 (5 min skew). | High | FIXED |
| Y2 | `paid_at` without an offset was silently read in the server zone (the existing check could never fire). Now 400. | Medium | FIXED |
| Y3 | **Money in payment responses was a float** (`balance`, `statement`, `pay-summary`, payment detail, allocations, reversals): `outstanding_amount: 1000.0`, risk of 0.1+0.2 style errors in clients. Now exact strings (`apps/common/serializers.decimals_to_str`). Existing tests were updated (`tests/payment/helpers.py` parses strings back). | High | FIXED |
| Y4 | Unknown fields accepted on payment, reverse and allocate. Now 400. | Low | FIXED |
| — | Verified OK: oldest purchase paid first, over-payment becomes advance and is never blocked, partial reversals, reversal and allocation idempotency (201 / 200 / 409), 5 parallel reversals of 60 on a 100 payment: exactly one wins, parallel payments allocate every rupee once, duplicate payment numbers are one winner, statement closing balance equals balance, staff blocked. | | |
| N6 | `decimals_to_str` is only applied to the payments API. The same float problem is likely in sales, stock, expenses, reports and billing JSON: check each module. | Medium | NOTE |

## 7. Routes, standing orders, runs & entries (`apps/routes`) — tests: `tests/routes/test_routes_qa.py`

Strong already (65 tests pass; duplicate route name, price override by staff, parallel start, parallel entry were all found correct).

| # | Bug | Severity | Status |
|---|---|---|---|
| T1 | Unknown fields accepted on every routes serializer (route, customers, standing order, run start, entry, items, reopen). Now 400. | Low | FIXED |
| T2 | A route's customer list accepted the same customer twice (silently merged) and an unlimited list; an entry accepted unlimited items. Now 400; max 2000 customers, 50 items. | Low | FIXED |
| T3 | **There is no endpoint to rename or deactivate a route** (only create and set customers). A wrong name or a closed route stays forever. | Medium | OPEN (feature gap: needs `PATCH routes/<id>/`; say if you want it) |
| T4 | Route list is not paginated (fine for a handful of routes per shop; revisit for chains). | Low | NOTE |

## 8. Deliveries (`apps/deliveries`) — tests: `tests/delivery/test_delivery_api_qa.py`

Strong already (90 delivery tests pass: price snapshot, no client price on priced products, shortfall flagged not blocked, replay by `client_ref`, cancel / correct keep stock right, parallel same ref delivers once, cancel vs correct race).

| # | Bug | Severity | Status |
|---|---|---|---|
| D1 | `delivered_at` without an offset was guessed in the server time zone. Now 400 (`StrictDateTimeField`). | Medium | FIXED |
| D2 | Unknown fields accepted (a client sending `line_total` or a typo was silently ignored). Now 400 on create, cancel and correct. | Low | FIXED |
| D3 | The delivery detail's `stock_movements[].quantity` was a float (`2.0`). Now a string. | Low | FIXED |
| N7 | One delivery per customer + product + shift + day is enforced (a second one is a 400). A customer who takes extra milk in the same shift must have the first entry corrected, not a second added. Confirm this is the business rule. | Medium | OPEN |

## 9. Shop sales (`apps/sales`) — tests: `tests/sales/test_sales_qa.py`

The most carefully built module (strict body checks, `client_ref` required, string money, 77 tests pass incl. races). The sweep found only:

| # | Bug | Severity | Status |
|---|---|---|---|
| H1 | A cancel / correct **reason** of any length and a sale **note** of 100,000 characters were accepted. Now max 500 / 1000. | Low | FIXED |
| — | Verified OK: future `sold_at`, naive timestamps, CREDIT needs a same-business active customer, replay (same body) and conflict (409), unknown / wrong-type fields all 400, tenant isolation, roles, list / summary params, quick-customer validation. The N1 note (quick-customer numbering scan, duplicate walk-ins) is still open. | | |

## 10. Customer bills & customer payments (`apps/billing`) — tests: `tests/billing/test_billing_qa.py`

Already audited and heavily tested (period freeze, finalize / void, allocation, races, `audit_bills`). The sweep added:

| # | Bug | Severity | Status |
|---|---|---|---|
| B1 | Void / reverse / allocate **reason** and payment **notes** had no length limit (500 / 1000 now). | Low | FIXED |
| B2 | Bill list with an unknown `status` or `bill_type` silently returned an empty page (looked like "no bills"). Now 400. | Low | FIXED |
| N8 | `apps/billing` already has `jsonable_money`; `apps/common/serializers.decimals_to_str` duplicates it. Merge later (no behaviour difference). | Low | NOTE |
| N9 | Bulk bill generation loops over every customer inside one request. Fine for hundreds of customers; for a shop with thousands, run it as a background job. | Medium | NOTE (scale) |

## 11. Stock, wastage / adjustments & production (`apps/inventory`) — tests: `tests/inventory/test_stock_qa.py`

Same strict style as sales (unknown / missing fields rejected, shortfall flagged, 71 tests pass).

| # | Bug | Severity | Status |
|---|---|---|---|
| K1 | A production batch **note** of any length was accepted (create and correct). Now max 1000. | Low | FIXED |
| — | Verified OK: all adjustment and batch validation cases return 400 (kinds, quantities, future / naive times, unknown product, untracked product, input = output product), staff read-only, outsiders blocked, quantities are strings, balance list uses a constant number of queries. | | |

## 12. Expenses, employees & salary (`apps/expenses`) — tests: `tests/expenses/test_expenses_qa.py`

Strict module (strict bodies, owner-only salary, pro-rata by calendar days, over-payment becomes advance, replay / conflict). The sweep found:

| # | Bug | Severity | Status |
|---|---|---|---|
| E1 | Cancel / correct **reason** had a minimum but no maximum (any length stored). Now max 500. | Low | FIXED |
| E2 | Employee **phone** accepted any text (create and PATCH). Now the same rules as customers / suppliers. | Low | FIXED |
| E3 | Employee `joined_on` accepted any future date (2099). Now at most 366 days ahead (a hire starting next month is normal). | Low | FIXED |
| O4 | Expense categories can be created by OWNER **and** MANAGER; CLAUDE.md says "categories are owner-created". Left as built (a manager adding "Tea" is practical). Confirm the intent. | Low | OPEN |
| N10 | Replays of expense / salary-payment creates answer 201 (other modules answer 200 on replay). Harmless; unify when convenient. | Low | NOTE |
| — | Verified OK: future expense dates refused, text and money limits, over-payment is an advance and never blocked, generate-twice is idempotent (201 then 200), staff / manager blocked from all salary data, replay / conflict (409). | | |

## 13. Reports & dashboards (`apps/reports`) + date handling across the API — tests: `tests/reports/test_reports_qa.py`, `tests/reports/test_date_overflow_qa.py`

Reports were strong (owner / manager only, salary hidden, flat query counts, CSV / PDF, export throttle, row caps). The sweep found one serious cross-cutting bug:

| # | Bug | Severity | Status |
|---|---|---|---|
| X1 | **A date such as `9999-12-31` crashed with a 500** (`OverflowError` on `date + 1 day`) in 12+ endpoints: milk balance, closing stock, shop-sale list / summary, milk list / daily summary, stock detail, expenses list / summary, supplier statement + print + pay-summary, customer statement, bill generate, customer bill create. Dates now must be in 2000-2100 (`apps/common/dates.py`, `milk_queries._date_param`, billing serializers); a bad date is a 400. | **High** | FIXED |
| X2 | `milk-balance` and `closing-stock` silently ignored unknown query params (a typo returned the wrong report with a 200). Now 400, like the other reports. | Low | FIXED |
| N11 | Test `test_opening_balance_carries_what_happened_before_the_start` failed between 00:00 and 05:30 IST (it mixed the UTC date with the business date). Test fixed to use `timezone.localdate()`; no product bug. | Low | FIXED |
| N12 | The Django `TIME_ZONE` is Asia/Kolkata and every business also has its own `timezone`. For all-India shops that is fine; a business outside India works because day ranges use `Business.timezone`. No change. | — | NOTE |

## Owner decisions on the OPEN questions (22 Oct 2026)

| Question | Decision | Status |
|---|---|---|
| Fat limit (O3) | Buffalo fat must be between **5 and 20**, set in `.env` as `MILK_FAT_MIN` / `MILK_FAT_MAX` (restart after changing). A litres-only correction of an older entry keeps its recorded fat and is not re-checked. | DONE |
| Route rename / deactivate (T3) | `PATCH routes/<id>/` (name, assigned_to or null, is_active). Deactivating needs the route's open runs closed; inactive routes show with `?is_active=false` or `all`. Past runs and deliveries are untouched. | DONE |
| Expense categories (O4) | Owner and manager both create (as built). | DONE (no change) |
| Shared phone (O1) | Allowed. The create / patch response carries `warnings[0].customers` (up to 5: id, code, name) when another customer of the business has the same number. | DONE |
| Milk rate backdate (O2) | Owner-only correction of a past rate that re-prices the affected entries. Plan sent for approval. | PLAN |
| One delivery per day (N7) | Allow several deliveries per customer, product, shift and day; `client_ref` guards double taps. Plan sent for approval. | PLAN |
