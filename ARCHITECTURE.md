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
  businesses/          Business (tenant), BusinessMembership (roles), permission class, seed command
  products/            categories, products, effective-dated prices
  customers/           door-to-door / shop customers
  deliveries/          daily delivery entries (customer × product × qty)
  suppliers/           suppliers + per-milk-type fat rates
  purchases/           purchases (draft→received), purchase lines, milk collections
  payments/            supplier payments, allocations, reversals, statements
  inventory/           stock movement ledger
  sales/               (empty stub)
  reports/             (empty stub)
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
| `MilkCollection` | One milk intake: litres, fat, milk type, the rate applied (snapshot) and computed amount, linked 1:1 to a `SupplierPurchase`. Amount = `calculate_supplier_milk_amount(litres, fat, rate)`. |
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
| Milk purchase | `businesses/{b}/suppliers/{s}/milk-collections/` · `.../{collection_id}/` |
| Payments | `businesses/{b}/suppliers/{s}/payments/` · `suppliers/{s}/statement/` |
| Auth | see §5 |

Not yet exposed over the API (models/services exist): purchases CRUD + receive, stock balance, payment reversal/allocation.

## 8. Request flow examples

**Record a delivery** — `POST /businesses/{b}/deliveries/`
1. JWT authenticates → `IsBusinessMember` checks membership/role, sets `request.business`.
2. Serializer validates customer/product belong to the business.
3. `create_delivery` (atomic): looks up the effective price, snapshots `unit_price`, computes `line_total`, saves `Delivery`, writes a `DELIVERY` `StockMovement`.

**Record a milk collection** — `POST /businesses/{b}/suppliers/{s}/milk-collections/`
1. Same auth/tenant checks.
2. `create_milk_collection` (atomic): resolves the supplier's rate for that milk type and time, computes amount from litres × fat × rate, creates the linked purchase and the collection.

**Supplier statement** — `GET .../suppliers/{s}/statement/?start=&end=`
`get_supplier_account_statement` sums purchases vs. payments/allocations in the period and returns opening balance, entries, closing balance (positive = we owe supplier, negative = advance).

## 9. Configuration

Environment variables (see [.env.example](.env.example)): `SECRET_KEY`, `DEBUG`, `DB_*`, `JWT_SIGNING_KEY`, `EMAIL_*`, `DEFAULT_FROM_EMAIL`, `PASSWORD_RESET_OTP_TTL_MINUTES`, `REDIS_URL`, `NUM_PROXIES`. Local setup: [Readme.md](Readme.md). Demo data: `python manage.py seed_dairy_data`.

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
