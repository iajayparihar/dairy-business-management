# Architecture

Backend for a multi-tenant dairy business management system. It replaces the paper register a dairy shop keeps today: milk bought from village suppliers, milk delivered door-to-door, shop sales, dairy products, credit (udhaar) and payments.

Status legend: ✅ implemented · 🟡 partial · ❌ not started. See [ROADMAP.md](ROADMAP.md) for the plan.

## 1. Problem being solved

Gathered from the client conversation (owner of a dairy shop with door-to-door supply):

| Pain point | Today |
|---|---|
| Milk purchase | Suppliers bring milk daily; rate is per fat (e.g. ₹9/fat × fat% × litres), differs per supplier and for cow vs buffalo (cow sometimes a fixed rate/litre). Written by hand. |
| Door-to-door supply | ~120–150 customers, ~125 L/day. Delivery boy writes daily quantity on paper; month-end bill takes the owner 3–4 days by hand. |
| Shop sales + udhaar | Dairy products (paneer, dahi, mawa, ghee) and "other" items sold on credit; hard to track on paper, losses result. |
| Supplier payments | Paid every ~5 days, part cash/part online, with advances. Reconciled column by column by hand. |
| Visibility | No view of daily litres bought vs sold vs converted to ghee/dahi, no expense or salary tracking. |

Design principle from the client: **keep the front simple** (tap customer → litres → cash/online/credit); complexity stays behind the scenes.

## 2. System overview

```
 Mobile / tablet app (planned)          Admin (Django admin, exists)
          │  JWT (Bearer)                        │ session
          ▼                                      ▼
 ┌────────────────────────── Django 5.2 + DRF ───────────────────────────┐
 │ config/urls.py  →  /api/v1/...                                         │
 │                                                                        │
 │  views (thin)  →  serializers (validate/shape)  →  services (rules)   │
 │       │                                               │                │
 │  IsBusinessMember permission                     models + DB           │
 │  (tenant + role check)                           constraints           │
 └────────────────────────────────────┬───────────────────────────────────┘
                                      ▼
                         PostgreSQL (psycopg 3)     Redis (optional, throttle counters)
```

- **Stack:** Python, Django 5.2, Django REST Framework 3.18, SimpleJWT 5.5 (with token blacklist), PostgreSQL, `django-environ` for config, `django-cors-headers`.
- **Layering:** views stay thin; business rules live in `services.py` (writes, wrapped in `transaction.atomic`) and `selectors.py` / `statements.py` (reads). Rules that must never be violated are also enforced as DB constraints.
- **Locale defaults:** timezone `Asia/Kolkata`, currency `INR`.

## 3. Project structure

```
config/                settings, root urls, wsgi/asgi
apps/
  common/              SoftDeleteModel, tenant-aware querysets, validators
  users/               custom User, JWT login/logout, OTP password recovery
  businesses/          Business (tenant), BusinessMembership (roles), permission class, demo seed (seed/ + seed_demo)
  products/            categories, products, effective-dated prices
  customers/           door-to-door / shop customers
  deliveries/          daily delivery entries (customer × product × qty)
  suppliers/           suppliers + per-milk-type fat rates
  purchases/           purchases (draft→received), purchase lines, milk collections
  payments/            supplier payments, allocations, reversals, statements
  billing/             customer bills, customer payments, allocations, balance, statement, bill PDF
  inventory/           stock movement ledger
  sales/               shop counter sales
  reports/             milk balance and closing stock reports
  expenses/            expenses by category, employees, salary ledger
tests/                 cross-app tests (business, customer, delivery, payment, products, purchases, supplier, users)
```

Each app has its own `tests.py` plus the shared `tests/` tree.

## 4. Multi-tenancy and access control

- **Tenant = `Business`.** Every business-owned table has a `business` FK (`on_delete=PROTECT`), and every URL is nested under `/businesses/<uuid:business_id>/`.
- **`BusinessMembership`** links a user to a business with a role: `OWNER`, `MANAGER`, `STAFF`. Unique per (business, user).
- **`IsBusinessMember`** ([apps/businesses/permissions.py](apps/businesses/permissions.py)) resolves `business_id` from the URL, checks the user has an active membership and that the view's `required_business_roles` allows their role, then sets `request.business` / `request.business_membership`. Superusers can access any active business. Object-level checks confirm `obj.business_id` matches the request's business.
- **Tenant-scoped querysets** in `apps/common/querysets.py` so queries filter by business by default (covered by `tests/test_tenant_querysets.py`).
- Owner protection (cannot demote/remove the last owner) is enforced in `businesses/services.py` (`tests/test_owner_protection.py`).

## 5. Authentication

Base path `/api/v1/auth/` ([apps/users/auth_urls.py](apps/users/auth_urls.py)).

| Endpoint | Purpose |
|---|---|
| `POST token/` | Login → access (30 min) + refresh (7 days, rotating) |
| `POST token/refresh/` | Rotate refresh token (old one blacklisted) |
| `POST token/verify/` | Verify a token |
| `POST logout/` | Blacklist refresh token |
| `POST password/forgot/` | Email a 6-digit OTP |
| `POST password/reset/` | OTP + new password; revokes all refresh tokens |
| `POST password/change/` | Authenticated change with old password |

OTP design (`PasswordResetOTP`, [apps/users/services.py](apps/users/services.py)):
- OTP stored **hashed**, compared in constant time; TTL from `PASSWORD_RESET_OTP_TTL_MINUTES` (default 10).
- Row-locked (`select_for_update`) issue and reset; partial unique index allows one live OTP per user; resend cooldown.
- Throttles: login 10/min, forgot 5/h, reset 10/h, change 10/h. Counters need a shared cache (`REDIS_URL`) in production; set `NUM_PROXIES` behind a reverse proxy.
- Email backend: console in `DEBUG`, SMTP otherwise. `purge_password_otps` management command deletes OTPs older than 1 day.
- Known, accepted limitation: access tokens issued before a reset remain valid until they expire (≤ 30 min).

## 6. Data model

All IDs are UUIDs. Money and quantity are `Decimal` with fixed precision. "SD" = `SoftDeleteModel` (`is_deleted`, `deleted_at`, `deleted_by`; default manager hides deleted rows).

```
User ──< BusinessMembership >── Business (SD)
                                   │
   ┌──────────┬──────────┬─────────┼──────────────┬──────────────┐
   ▼          ▼          ▼         ▼              ▼              ▼
ProductCategory  Customer     Supplier        StockMovement   (all rows carry business FK)
   │               │             │
   ▼               │             ├──< SupplierMilkRate  (COW/BUFFALO, effective_from/to)
Product ──< ProductPrice         │
   │  (effective-dated,          ├──< SupplierPurchase ──< PurchaseLine ── Product
   │   no-overlap exclusion)     │        (DRAFT/RECEIVED/CANCELLED)
   │                             ├──< MilkCollection ──(1:1)── SupplierPurchase
   │                             │        litres, fat, applied_rate, amount, rate_record FK
   │                             └──< SupplierPayment ──< SupplierPaymentAllocation
   │                                        │ (CASH/UPI/BANK_TRANSFER/CHEQUE/OTHER)
   │                                        └──< SupplierPaymentReversal
   └──< Delivery ── Customer      (qty, unit_price snapshot, line_total, delivered_at)
```

Key tables and rules:

| Model | Notes |
|---|---|
| `Business` | Tenant. Unique slug, timezone, currency, `is_active`. |
| `BusinessMembership` | Role per user per business; indexed by (user, is_active) and (business, is_active, role). |
| `Product` / `ProductCategory` / `ProductPrice` | Prices are **effective-dated**; a PostgreSQL exclusion constraint prevents overlapping price windows per product. Changing a price goes through `change_product_price`. |
| `Customer` | `customer_code` unique per business; type `HOUSEHOLD` / `SHOP` / `OTHER`. |
| `Delivery` | One row per customer × product × day. `unit_price` is a snapshot of the price at delivery time; `line_total` is computed; check constraints on quantity/price/total. Created via `create_delivery`, which also writes a `DELIVERY` stock movement. |
| `Supplier` | Milk/product supplier per business. |
| `SupplierMilkRate` | Rate per milk type with `effective_from` / `effective_to`; resolved by `resolve_supplier_milk_rate`. |
| `MilkCollection` | One can of milk: milk type, shift, litres, the rate applied and `pricing_basis` (snapshots) and computed amount, linked 1:1 to a `SupplierPurchase`. BUFFALO = litres × fat × rate (`FAT`); COW = litres × rate (`FLAT`, fat not recorded). Label (`collection_number`) is server-generated, e.g. `RAM01-261015-M-B2`. Never deleted: `status` RECORDED / CANCELLED with reason, `replaces` links a correction. See §15. |
| `BusinessMilkConfig` | Per business: which product holds raw cow / buffalo milk stock. |
| `SupplierPurchase` / `PurchaseLine` | Draft → Received lifecycle. Receiving writes `PURCHASE_RECEIPT` stock movements (unique per line). |
| `StockMovement` | Append-only ledger: `PURCHASE_RECEIPT`, `DELIVERY`, `WASTAGE`, `ADJUSTMENT_IN/OUT`, `REVERSAL_IN/OUT`. Stock balance = sum of movements. Partial unique constraints stop double-posting a receipt or delivery. |
| `SupplierPayment` | Immutable ledger row (custom queryset blocks update/delete). Supports advances. |
| `SupplierPaymentAllocation` | Allocates payments (including advances) against purchases. |
| `SupplierPaymentReversal` | Corrections are new rows, never edits. |

Design choices on the ledger tables: payments and stock movements are **append-only**; mistakes are fixed with reversals so history stays auditable. Prices and rates are **snapshotted** onto transactions so later rate changes never rewrite past bills.

## 7. API surface (all under `/api/v1/`)

| Area | Endpoints |
|---|---|
| Products | `businesses/{b}/categories/` · `products/` · `products/{id}/` |
| Customers | `businesses/{b}/customers/` · `customers/{id}/` |
| Deliveries | `businesses/{b}/deliveries/` (list/create) |
| Suppliers | `businesses/{b}/suppliers/` · `suppliers/{id}/` · `suppliers/{s}/milk-rates/` |
| Milk purchase | `businesses/{b}/suppliers/{s}/milk-collections/` · `.../{id}/` · `.../{id}/cancel/` · `.../{id}/correct/` |
| Milk (business-wide) | `businesses/{b}/milk-collections/` · `.../daily-summary/?date=` · `businesses/{b}/milk-config/` |
| Payments | `businesses/{b}/suppliers/{s}/payments/` · `suppliers/{s}/statement/` · `.../statement/print/` · `.../pay-summary/` · `.../balance/` · `businesses/{b}/payments/{id}/` · `.../reverse/` · `.../allocate/` |
| Auth | see §5 |

Not yet exposed over the API (models/services exist): purchases CRUD + receive, stock balance, payment reversal/allocation.

## 8. Request flow examples

**Record a delivery** — `POST /businesses/{b}/deliveries/`
1. JWT authenticates → `IsBusinessMember` checks membership/role, sets `request.business`.
2. Serializer validates customer/product belong to the business.
3. `create_delivery` (atomic): looks up the effective price, snapshots `unit_price`, computes `line_total`, saves `Delivery`, writes a `DELIVERY` `StockMovement`.

**Record a milk collection** — `POST /businesses/{b}/suppliers/{s}/milk-collections/`
1. Same auth/tenant checks.
2. `create_milk_collection` (atomic, business row locked): resolves shift and label, replays an identical `client_ref`, resolves the supplier's rate for that milk type and time, computes the amount (see §15), creates the linked purchase and the collection, posts a `PURCHASE_RECEIPT` stock movement when a product is mapped, then auto-applies supplier advances.

**Supplier statement** — `GET .../suppliers/{s}/statement/?start_date=&end_date=&page=&page_size=`
`get_supplier_account_statement` sums purchases vs. payments/allocations in the period and returns opening balance, entries, closing balance (positive = we owe supplier, negative = advance).

## 9. Configuration

Environment variables (see [.env.example](.env.example)): `SECRET_KEY`, `DEBUG`, `DB_*`, `JWT_SIGNING_KEY`, `EMAIL_*`, `DEFAULT_FROM_EMAIL`, `PASSWORD_RESET_OTP_TTL_MINUTES`, `REDIS_URL`, `NUM_PROXIES`. Local setup: [Readme.md](Readme.md). Demo data: `python manage.py seed_demo` (DEBUG only; `--fresh` wipes the DB first; one module per area in `apps/businesses/seed/`).

## 10. Technical decisions (and why)

| Decision | Reason |
|---|---|
| Shared-schema multi-tenancy via `business` FK | Simple, one DB, cheap to run; isolation enforced in permission class + scoped querysets + tests. |
| Service layer for writes | Keeps multi-step rules (price lookup + stock + totals) atomic and reusable from API, admin and seed command. |
| UUID primary keys | Safe to expose in URLs; no tenant enumeration. |
| Append-only ledgers + reversals | Money and stock must be auditable; the client's current pain is silent errors. |
| Snapshot prices/rates on transactions | Rate changes (e.g. ghee ₹400→₹450) must not alter old bills. |
| DB constraints for invariants | Check/unique/exclusion constraints catch bugs that slip past app code. |
| Soft delete for master data | Keeps history and FKs intact. |
| OTP in DB, no new dependency | One live OTP per user, row-locked; avoids extra infra beyond optional Redis for throttling. |
| DRF + JWT, mobile-first | Client wants a phone/tablet app for entry; no laptop. |

## 11. Testing

Django `TestCase` suites across apps (≈190 tests at last full run, all passing), including concurrency tests for OTP and payment paths that fail if row locking is removed. Run: `python manage.py test`.

## 12. Not built yet (architecture-level)

Sales/POS module, reports/dashboards, customer billing + monthly statements, customer payments/credit ledger, production (dairy conversion) tracking, expenses/salary, notifications (SMS/WhatsApp), frontend app. Details in [ROADMAP.md](ROADMAP.md).

## 13. Stock ledger timestamp policy

`StockMovement` carries two timestamps with different meanings:

| Field | Meaning | Who sets it |
|---|---|---|
| `occurred_at` | Business time: when the stock physically moved | The operation (delivery time, receipt time) |
| `created_at` | Posting time: when the row was written | The database, never editable |

Rules:

1. Movements are append-only. A correction never edits or deletes a row.
2. A reversal copies `occurred_at` from the movement it reverses. The original and its reversal cancel out on the original business date. `created_at` shows when the correction was made.
3. A correction (cancel + replacement) therefore posts: reversal at the original `occurred_at`, new movement at the delivery's original `delivered_at`.
4. Reports choose a basis explicitly (`inventory.services.stock_net_by_date`):
   - `business`: grouped by `occurred_at`. Use for daily milk balance, production and closing stock (Phase 7). A past day's figure changes when it is corrected, which is the truthful figure.
   - `posted`: grouped by `created_at`. Use for audit ("what was written on day X"). A closed day's posted total never changes later.
5. Both bases always sum to the same total over all time.
6. `Delivery.stock_shortfall` is a snapshot of stock at entry time, not live.

## 14. Routes and daily runs (Phase 2)

`apps/routes`: `Route` (usual delivery boy = a STAFF `BusinessMembership`), `RouteCustomer` (a customer is on one route, ordered), `StandingOrder` (usual basket per shift), `RouteRun` (route + date + shift, OPEN/CLOSED), `RouteVisit` (one expected stop: PENDING, DELIVERED, NOT_DELIVERED, ABSENT, SKIPPED). `Delivery.visit` links money rows to the stop.

- Absent or skipped never creates a zero-value delivery; the visit records why.
- `record_entry` is one transaction per customer: all deliveries of the entry commit or none; `client_ref` makes retries safe.
- A run closes only when no visit is PENDING. Closed runs block cancel/correct until an owner/manager reopens with a reason.
- Cancelling a visit's only delivery puts the visit back to PENDING.
- Endpoints under `/api/v1/businesses/{id}/`: `routes/`, `routes/{id}/customers/`, `standing-orders/`, `route-runs/`, `route-runs/{id}/tasks|entries|close|reopen/`, `deliveries/{id}/` and `deliveries/{id}/cancel/`.

## 15. Milk collections (Phase 3)

- **Pricing**: BUFFALO litres × fat × rate; COW litres × rate. A COW request containing `fat` (even 0) is rejected (400); BUFFALO needs fat > 0. `pricing_basis` is snapshotted, so old rows keep their meaning.
- **One row per can**, many per supplier / shift / day. `shift` may be sent; if omitted it is derived from `collected_at` in `Business.timezone` (before 15:00 = MORNING).
- **Label** `<SUPPLIER_CODE>-<YYMMDD>-<M|E>-<C|B><seq>` is generated under the business lock; `seq` counts every row ever written (cancelled included), so it is never reused. Clients cannot send it.
- **Retry safety**: `client_ref` unique per business. Same ref + identical request returns the original (HTTP 200, nothing new written); same ref + any difference is HTTP 409.
- **Stock**: `BusinessMilkConfig` maps cow / buffalo to products (same business, tracked, litres, different products). Mapped → a receipt at `collected_at`; unmapped → collection still succeeds and `stock_posted=false`. Adding a mapping never back-posts.
- **Cancel / Correct** (OWNER/MANAGER): reason required (≥ 3 chars); rows are kept. Cancel marks the entry and its purchase CANCELLED and posts a `REVERSAL_OUT` at the receipt's `occurred_at`. Correct = cancel + a replacement at the original time and shift (rate resolved at that time), in one transaction. If the milk was already used and stock goes negative the action still succeeds and `cancel_stock_shortfall` records the missing litres (same policy as deliveries). Paid entries can be cancelled: balances and the allocator ignore allocations on cancelled purchases, so the money returns to the supplier's credit and is reusable.
- **Lock order**: business (`select_for_update(no_key=True)`) → purchase → collection → product. `allocate_supplier_advance` locks purchase → payment, so a payment can never be allocated to a purchase being cancelled. Never take a plain `FOR UPDATE` on the Business row: it blocks every insert that references the business and deadlocks against purchase/payment locks.
- **Reports**: `daily-summary` uses half-open business-local days `[start, next_day_start)`, excludes cancelled entries, and reports a litre-weighted average fat over buffalo only. Lists are paginated and filterable (`start_date`, `end_date`, `shift`, `milk_type`, `status`, `supplier`). All milk endpoints are OWNER/MANAGER only (cost data).
- **Known limitation**: a cancelled purchase disappears from supplier statements for every date (statements filter `status=RECEIVED`), including statements previously issued for an earlier date.

## 16. Supplier payments, reversals and statements (Phase 4)

- **Payment**: immutable; `client_ref` unique per business (same ref + identical request = 200 replay, any difference = 409). Duplicate `payment_number` is a 400. `POST .../suppliers/<id>/payments/` auto-allocates oldest purchase first (service `auto_allocate=True`); the rest stays as supplier advance.
- **Reversal** (`SupplierPaymentReversal`, many per payment): full or partial; the sum never exceeds the payment (checked under the payment row lock). The reversed amount comes out of the unallocated advance first, then unwinds allocations newest-first, each recorded as a `SupplierPaymentAllocationReversal` row. Allocation rows are never edited: effective allocation = `amount - sum(unwinds)`. `reversed_at` is `max(now, paid_at)`. Allocations on cancelled purchases are ignored (money already returned to credit) and are never unwound.
- **Amounts**: `get_payment_amounts` → `amount`, `reversed`, `net = amount - reversed`, `allocated`, `unallocated = net - allocated`. These are different quantities and are never interchanged.
- **One arithmetic**: `selectors.get_supplier_position(as_of)` feeds both `get_supplier_balance` and the statement summary. `outstanding_amount = purchases + adjustments - allocated` is not clamped (negative = refund due) and is not netted against advance (`available_credit`). Statement `closing_balance == outstanding_amount - available_credit`.
- **Time**: statement days are half-open `[day 00:00, next day 00:00)` in `Business.timezone`. An allocation counts from `max(paid_at, purchased_at)`; reversals and their unwinds from `reversed_at`.
- **Lock order**: supplier (`no_key`) → purchases (pk order) → payment. Reverse takes only the payment lock; unwinding only frees purchase capacity, so it cannot overpay, and a purchase cancelled meanwhile just makes that allocation irrelevant.
- **API** (`/api/v1/businesses/<id>/`, OWNER/MANAGER only): `GET payments/<pid>/`, `POST payments/<pid>/reverse/` `{amount?, reason, client_ref?}` (201 / 200 replay / 409 / 400), `POST payments/<pid>/allocate/` `{purchase_id, amount}` (201 / 200 identical retry / 400), `GET suppliers/<sid>/balance/`. Another business's payment id is 404.
- **Migrations**: `0004` schema, `0005` backfill (one unwind row per allocation of every pre-existing reversal, full amount; no existing row touched). Reversing 0004 back to a one-to-one reversal fails if a payment has several reversals.
- **Rule for code**: post reversals only through `reverse_supplier_payment`. A bare `SupplierPaymentReversal` row does not unwind allocations.
- **Pay for period** (`GET suppliers/<sid>/pay-summary/`, OWNER/MANAGER): `?days=N` (1-366, default 5, includes today) or both `start_date`/`end_date`; mixing them, one date alone, a bad format or an inverted range is a 400. Period days are half-open in `Business.timezone`. `suggested_amount = max(outstanding - available_credit, 0)` (everything owed, older dues included, less advance already held); a negative outstanding is shown as `refund_due`. `breakdown` splits `period_outstanding` from `outstanding_outside_period`; they always sum to `outstanding_amount`. Read-only and lock-free: `POST payments/` re-checks live data, and an overpayment just becomes advance.
- **Statement API** (OWNER/MANAGER only, as are the payments list, print and pay-summary): `transactions` is one page (default 50, max 200) of the period, each row's running `balance` computed over the whole period, so the last row of the last page equals `closing_balance`; totals are period-wide; `pagination` = `{count, page, page_size, num_pages}`. Also returns `business_name`, `currency`, `timezone`, `supplier_code`, `supplier_name`, `generated_at`. `statement/print/` returns escaped, business-local HTML (all rows, default last 30 days, max 366, `Cache-Control: no-store`); the frontend fetches it with the bearer token and prints it. The payments list is paginated with `StandardPagination`.

## 17. Customer billing and payments (Phase 5)

`apps/billing`. The receivable is a **FINALIZED `CustomerBill`**; deliveries are only its source. Positive balance = the customer owes, negative = advance.

- **Models**: `CustomerBill` (DRAFT / FINALIZED / VOID; type PERIOD or OPENING; explicit `period_start`/`period_end`; `total_amount`, `balance_before` and `header_snapshot` fixed at finalize; `replaces` links a replacement), `CustomerBillLine` (one immutable row per delivery, amount = the delivery's stored `line_total`, milk kind snapshotted from `BusinessMilkConfig`), `CustomerPayment`, `CustomerPaymentAllocation`, `CustomerPaymentReversal`, `CustomerPaymentAllocationReversal` (same shape as the supplier tables).
- **One arithmetic**: `selectors.get_customer_position(as_of)`. `net_balance = total_billed - total_payments = outstanding_amount - available_credit`. Allocations on VOID bills are ignored (money returns to credit). `as_of` is void-aware, so an old statement reads the same after a later void. `unbilled_*` (deliveries with no line on a FINALIZED bill) is reported beside the balance, never in it.
- **Lifecycle**: a DRAFT stores no lines (live from deliveries, so never stale). `finalize` needs the period to have ended (`period_end` < business-local today), accepts `expected_total` (409 if the live total differs), copies the lines, snapshots `balance_before`, and applies held advance to the new bill. `void` needs a reason (>= 3 chars), is allowed even when paid, and by default issues a replacement DRAFT. Numbers are server-generated and never reused: bill `<CODE>-<YYMMDD of start>-<seq>` (opening `<CODE>-OB-<YYMMDD>-<seq>`), payment `PAY-<CODE>-<YYMMDD>-<seq>`.
- **Period lock**: a FINALIZED bill closes that customer's period. `create_delivery`, `cancel_delivery` and `correct_delivery` refuse a delivery dated inside it (`billing.guards.assert_period_open`, day read in `Business.timezone`) until the bill is voided. `Delivery.delivery_date` follows the server timezone, so bills filter `delivered_at` with half-open business-local bounds instead. DB guard: exclusion constraint, finalized periods of one customer never overlap.
- **Lock order**: route run -> customer (`FOR NO KEY UPDATE`) -> bill -> delivery -> payment -> product. Every writer of a customer's deliveries, bills or payments takes the customer lock first (`billing.locks.lock_customer`). Forced-interleaving tests: `tests/billing/test_concurrency.py`.
- **Payments**: `allocations` omitted = oldest bill first, `[]` = keep all as advance, list = exactly those; `paid_at` optional (default now, not future); replay compares the normalized request and ignores a defaulted time. Reversal takes advance first, then newest allocations, as explicit unwind rows. `payment_number` cannot be sent.
- **Opening balance**: owed amount = a FINALIZED OPENING bill (one live per customer; identical retry = 200, different = 409; void to re-enter); prepaid = advance payment (method OTHER).
- **API** (`/api/v1/businesses/<id>/`, OWNER/MANAGER only, unknown fields rejected, money as strings): `customers/<c>/payments/` (GET list, POST), `customers/<c>/balance/`, `customers/<c>/statement/`, `customers/<c>/bills/` (POST draft), `customers/<c>/opening-balance/`, `customer-payments/<p>/` (+ `reverse/`, `allocate/`), `bills/` (list: customer, status, bill_type, start_date, end_date, unpaid), `bills/generate/` (bulk drafts, `skipped` with reasons), `bills/<b>/` (+ `finalize/`, `void/`, `pdf/?lang=en|hi`, default `Business.bill_language`, other values 400). `customer-payments/` is separate because `payments/<id>/` is supplier payments.
- **Also changed**: customers with deliveries, bills or payments cannot be deleted (deactivate instead); `DeliveryAdmin` is read-only; `audit_bills` reports broken invariants (bill vs deliveries, allocations, balance reconciliation).
- **Bill PDF**: English ("Rs.") or Hindi ("₹", dd-mm-yyyy); every cell goes through `apps.reports.pdf.cell`, so any Devanagari or ₹ is shaped by Pillow/Raqm (transparent image). Labels live in `LABELS`; still byte-identical per language.
- **Known limits**: `correct_delivery` still fails for an inactive customer (reactivate first); no discounts or write-offs.
- **Migration**: `billing/0001_initial` only (schema, no backfill, no other table touched); rehearsed forward, reverse, forward on a copy of the dev DB.

## 18. Shop counter sales (Phase 6)

`apps/sales`. A `Sale` (CASH / ONLINE / CREDIT, COMPLETED / CANCELLED) has `SaleLine`s: a product line (price from the price list at `sold_at`, or typed for open-price products) or an "other item" (description + amount, no product, no stock). Rows are never edited or deleted; cancel needs a reason, correct = cancel + replacement at the original `sold_at` (`replaces`).

- **Ledger rule**: only CREDIT sales are billed. `CustomerBillLine` has exactly one source (DB check): `delivery` or `sale_line` (`product` is null for an other item, `shift` blank). Billing selects COMPLETED credit sales by `sold_at` with the same half-open business-local bounds as deliveries. CASH / ONLINE sales (even with a customer) never touch bills, payments or balance. Unbilled credit sales are reported beside the balance (`unbilled_sales`, included in `unbilled_amount`), never in it. A voided bill makes its sales unbilled again and the replacement draft picks them up.
- **Period freeze**: a CREDIT sale (create / cancel / correct) inside a FINALIZED period is refused via `assert_period_open`; cash / online are not frozen.
- **Stock**: `track_stock` products post a `SALE` movement at `occurred_at = sold_at`; shortfall = quantity beyond balance, stored on the line, never blocks. Cancel posts `REVERSAL_IN` at the original time. Responses carry `stock_shortfall` and `stock_posted` per line.
- **Numbers**: `SHOP-<YYMMDD in Business.timezone>-<seq>` (server-only, unique per business, never reused); `client_ref` required, unique per business; same ref + different data = 409.
- **Lock order**: business (`no_key`, number allocation and `client_ref` serialization) -> customer (`no_key`) -> sale -> products sorted by id (a correction locks old and new products together). Billing never takes the business lock, so no inversion. Tests: `tests/sales/test_concurrency.py`.
- **Roles**: any member sells, lists, adds a quick customer; STAFF may backdate 48 h (same as deliveries); cancel / correct OWNER/MANAGER. The customer of a sale cannot change in a correction (a customer-less sale may be given one).
- **API** (`/api/v1/businesses/<id>/sales/`, unknown fields rejected, money as strings): `` (GET list: start_date, end_date, payment_mode, status, customer_id, has_shortfall; POST), `<id>/`, `<id>/cancel/`, `<id>/correct/`, `summary/?date=`, `quick-customer/`.
- **Migrations**: `sales/0001`, `inventory/0007` (SALE type, `sale_line` FK), `billing/0002-0004` (nullable `delivery` / `product`, `sale_line`, one-source check); schema only, no backfill; rehearsed forward / back / forward on a copy of the dev DB.

## 19. Stock, production and stock reports (Phase 7)

`apps/inventory` (ledger, adjustments, production, stock API) and `apps/reports` (read-only reports).

- **Stock balance API** (`/api/v1/businesses/<id>/stock/`, any member, quantities only): list of tracked products (`search`, `negative=true`) and `<product_id>/` with the paged movement list (`from`, `to` = business-local days). One query for all balances (`stock_balances`, optional `before`); `stock_balance` stays the single-product source. `stock_net_by_date` takes a `tz` (pass `Business.timezone`).
- **Wastage / adjustments** (`stock/adjustments/`, OWNER/MANAGER): `StockAdjustment` kind WASTAGE / LOSS (stock out) or GAIN (stock in), reason (min 3 chars) required, number `ADJ-<YYMMDD>-<seq>`, `client_ref` required. Posts WASTAGE / ADJUSTMENT_OUT / ADJUSTMENT_IN at `occurred_at` (default now, not in the future). Shortfall on an out movement is stored (`stock_shortfall`) and never blocks. Cancel (reason) posts the reverse at the original time; cancelling a consumed GAIN makes stock negative and sets `cancel_stock_shortfall`. No correct: cancel + new entry. Product must be same business, active, `track_stock`.
- **Production** (`stock/production/`, OWNER/MANAGER): `ProductionBatch` with `ProductionLine`s (INPUT / OUTPUT), free-form, 1..20 lines per side, a product only once per batch and never on both sides, all tracked and active. Inputs post `PRODUCTION_OUT`, outputs `PRODUCTION_IN` at `produced_at`. No unit conversion, no recipes: `per_100_litres` yield is display only (when all inputs are litres). Input shortfall is stored per line, never blocks. Cancel reverses every movement at its original time (`cancel_has_shortfall` when an output was already used); correct = cancel + replacement at the original `produced_at` (`replaces`). Numbers `PRD-<YYMMDD>-<seq>`; `client_ref` replay compares notes, explicit time and the sorted (kind, product, quantity) list, mismatch = 409.
- **Lock order** for adjustments and production: business (`no_key`) -> adjustment / batch -> products sorted by id. Same as milk and sales. Tests: `tests/inventory/test_adjustment_concurrency.py`, `test_production_concurrency.py`.
- **Daily milk balance** (`reports/milk-balance/?date=` or `from`+`to`, max 62 days, OWNER/MANAGER): from the ledger of the mapped cow and buffalo products, per business-local day on `occurred_at`; `opening + in - out = closing` equals `stock_balance`. A reversal counts in the bucket of the movement it reverses. Buckets: bought, produced, adjust_in, delivered, sold_shop, converted, wastage, adjust_out. `unposted_bought_litres` = recorded collections minus ledger receipts (milk bought before a mapping existed is never back-posted); `mapping` shows which types are mapped.
- **Closing stock** (`reports/closing-stock/?month=YYYY-MM&compare=true`, OWNER/MANAGER): per tracked product opening / closing quantity for the business-local month, cost = PURCHASE-type `ProductPrice` effective at the last instant of the month, value = quantity x cost. No cost price = listed under `unvalued`, never counted as zero. `compare` gives quantity and value change against the previous month and `stock_gain_loss` (products valued in both). Live: a backdated correction changes a past month (section 13). `change_product_price` forbids backdating, so months before the first cost price are unvalued. The owner sets a cost price with `POST products/<pk>/prices/` (`price_type=PURCHASE`).
- **Migrations**: `inventory/0008` (StockAdjustment, `stock_adjustment` FK), `inventory/0009` (ProductionBatch / ProductionLine, PRODUCTION_IN / OUT types, `production_line` FK); schema only, no backfill; rehearsed forward / back / forward on a copy of the dev DB.

## 20. Audit hardening pass (after Phase 7)

Found by fuzzing every endpoint (garbage bodies and query strings, anonymous, STAFF and cross-tenant access), a static scan and a code read. Each item has a regression test in `tests/common/`, `tests/products/test_price_api.py` or `tests/users/test_throttle_spoofing.py`.

- **Product price API** (`/businesses/<id>/products/<pk>/prices/`): the price service existed but no endpoint did, so selling and cost prices could only be set in the admin. GET lists the history (STAFF see SELLING only; PURCHASE cost prices are OWNER/MANAGER), POST schedules a price (OWNER/MANAGER, `effective_from` optional = now, never backdated). A missing or inactive product is a 400, not a 500.
- **Malformed input is a 400, never a 500**: `parse_date` / `parse_datetime` raise on well-formed but impossible values (month 13, 31 Feb), which crashed the date filters and `sold_at` / `occurred_at` / `produced_at` / `collected_at`. Use `milk_queries._date_param` for dates in query strings. Routes filters (`customer`, `date`, `status`) are validated. `RejectNullBytesMiddleware` returns 400 for a NUL character in any query value or JSON string (PostgreSQL text cannot hold it).
- **Throttle bypass**: `NUM_PROXIES=0` was converted to `None`, which makes DRF trust the client-supplied `X-Forwarded-For`; rotating it reset the login and password-reset throttles. 0 now means "no proxy, use REMOTE_ADDR"; behind a proxy set `NUM_PROXIES` to the number of trusted proxies.
- **Cost data**: supplier milk rates are OWNER/MANAGER only for reads too (they were readable by STAFF).
- **Ledger integrity**: `StockMovement` rejects queryset `update` / `delete` / `bulk_update` (like the other ledgers); `SupplierPurchase` admin is read-only; `update_membership` takes `FOR NO KEY UPDATE` on the Business row like every other writer.
- **Closing stock** keeps a soft-deleted product that still holds stock (`product_deleted: true`) so the total reconciles with the ledger.
- **Known, not changed**: `Delivery.delivery_date` is derived in the Django timezone (Asia/Kolkata), not `Business.timezone` (billing and reports use `delivered_at`, so only a non-IST business would notice); production security settings (HTTPS redirect, HSTS, secure cookies) are not env-driven yet.


## 21. Expenses and salary (Phase 8)

`apps/expenses`. Everything money-like is append-only: a row is cancelled with a reason (who, when) or corrected, never edited or deleted (`CancellableLedgerRow`: `save()` only accepts the cancel fields, bulk update / delete raise, admins are read-only).

- **Expenses** (`/api/v1/businesses/<id>/expenses/`, OWNER/MANAGER): `Expense` has a category (`ExpenseCategory`, owner-created, name unique ignoring case, deactivate instead of delete), `expense_date` (a local calendar date in `Business.timezone`, past allowed, never future, months are not frozen), amount, payment method (same values as supplier / customer payments), free-text `payee`, number `EXP-<YYMMDD of expense_date>-<seq>` (server-only, never reused). `client_ref` is required: replay compares category, amount, method and texts, and the date only when it was sent. Cancel needs a reason; correct = cancel + replacement on the **original date** (`replaces`); `expense_date` and `client_ref` are rejected in a correction. `expenses/summary/?from&to` = totals per category (POSTED only, max 366 days, default last 30).
- **Salary** (OWNER only, reads included): `Employee` (code `EMP-<n>`, `monthly_salary`, `joined_on`, `left_on`). No allocations: one running figure per employee, `balance = salary + bonus - deductions - payments` (cancelled rows excluded). Positive = owed to the employee, negative = advance. An overpayment is never blocked, it shows as an advance and is netted by the next month. Cancelling an accrual that was already paid also just shows as an advance.
- **Accruals** (`SalaryAccrual`, kind SALARY / BONUS / DEDUCTION): the owner presses `POST salary/accruals/generate/ {month: "YYYY-MM", employee_ids?}`; it is safe to repeat (DB: one live SALARY per employee and month) and returns `created` plus `skipped` with a reason. Salary is pro-rata by calendar days when the employee joined or left inside the month (`monthly_salary * days / days_in_month`, rounded half-up; the day count and month length are stored on the row, so changing the salary later never recomputes old months). Skipped: inactive without `left_on`, not joined yet, already left, already generated. A future month is refused. BONUS / DEDUCTION are typed by the owner with a reason (`employees/<id>/adjustments/`, `client_ref` required).
- **Payments** (`employees/<id>/payments/`, `salary-payments/<id>/`, `.../cancel/`): number `SAL-<EMPLOYEE_CODE>-<YYMMDD>-<seq>`; `paid_at` default now, never future; an inactive employee can still be paid (final settlement). Cancel only (no partial reversal).
- **One arithmetic**: `selectors.employee_position(employee, as_of=None)`. The live figure and the event replay (`employee_events`: an accrual counts from the first day of its month, a payment from `paid_at`, every cancellation from the moment it was made) are asserted equal in tests. `employee_statement` returns opening / closing balance and every event with a running balance (unpaginated, max 366 days). `employee_balances` gives all balances in two queries for the employee list. `salary_cost_by_month` is the hook for Phase 9 (salary is a cost in the month it belongs to, not the month it was paid).
- **Lock order**: business (`no_key`: numbers, `client_ref`, month generation) -> employee (`no_key`) -> accrual / payment; expenses: business -> expense. Cancelling an accrual or payment takes employee -> row. These are leaves of the project-wide order. Tests: `tests/expenses/test_concurrency.py`.
- **Migrations**: `expenses/0001_initial` only (schema, no backfill, no other table touched).
- **Not built**: recurring expenses, bill photos, attendance-based deduction, links between an employee and an app login or an expense and a supplier.


## 22. Dashboard and reports (Phase 9)

`apps/reports`, read-only, live (a later cancel or correction changes past figures, same policy as section 13). Roles: OWNER/MANAGER; salary is OWNER only.

- **Daily summary** (`reports/dashboard/daily/?date=`): `dashboard.period_totals` over `[start, end)` in `Business.timezone`. Litres bought / milk cost from RECORDED `MilkCollection`; other purchases from RECEIVED `SupplierPurchase` that has no milk collection, plus `PurchaseAdjustment` by `occurred_at` (a milk collection owns a mirror purchase of equal amount: summing both would double count). Sales = DELIVERED deliveries (`delivered_at`) + COMPLETED shop sales (`sold_at`, all modes), independent of billing and payment. Litres sold = Cow/Buffalo products of `BusinessMilkConfig` only. Collections = shop CASH/ONLINE sales + customer payments minus reversals (reversals count on the day they are made); buckets cash / online (UPI, BANK_TRANSFER, CHEQUE) / other. End-of-day balances come from bulk positions; unbilled sales are shown beside them.
- **Profit** (`reports/dashboard/profit/`): `profit.profit`. Weeks are Mon-Sun, months are calendar months; the requested range is widened to whole buckets. `profit = sales - purchases - expenses - salary`; `profit_stock_adjusted` replaces purchases by `purchases - inventory_change`, where inventory_change is the PURCHASE-priced value of tracked stock at the end minus the start (products without a price are counted in `unvalued_products`, never valued at zero). Salary is a monthly figure: included in month buckets for OWNER only, never in week buckets. It is an operational estimate, not accounting profit (purchases are goods bought, not cost of goods sold).
- **Bulk positions** (`positions.py`): the same filters as `get_customer_position` / `get_supplier_position` in a fixed number of grouped queries; `tests/reports/test_positions.py` compares every party, now and at past instants.
- **Tables** (`tables.py`): supplier balances, supplier ledger (wraps `get_supplier_account_statement`), customer outstanding, stock (opening, per-type flow, closing, negative flag, reversals under the original type), product-wise sales (no discounts or returns exist, net = gross), expenses (POSTED only, no salary). `exports.py` formats the same table as JSON, CSV or (with `pdf.py`) PDF, so screen and file match. Query counts are asserted flat as data grows.
- **Export**: `?export=csv|pdf`. CSV is streamed, UTF-8 with BOM, plain decimals, local time, TOTAL row, text cells starting `= + - @ tab CR` get a leading apostrophe, safe filename, `no-store`. PDF: reportlab; Hindi runs are shaped by Pillow (Raqm) into images with bundled Noto Sans Devanagari (`apps/reports/fonts`, OFL); max 5,000 rows (400 above, use CSV); page numbers; throttled by `reports_export` (30/min/user, export requests only).
- **Not built**: weekly salary share, `@extend_schema` response docs for these views, a frontend (Phase 11).
