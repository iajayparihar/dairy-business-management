# Roadmap

What we are building: a mobile-friendly dairy management system that replaces the paper register (milk purchase, door-to-door supply, shop sales, credit, supplier payments, stock, expenses). Architecture is in [ARCHITECTURE.md](ARCHITECTURE.md).

Legend: ✅ done · 🟡 partial · ❌ not started

## Summary

| Phase | Theme | Status |
|---|---|---|
| 0 | Foundation (tenancy, auth, roles) | ✅ |
| 1 | Catalog: products, prices, customers | ✅ |
| 2 | Door-to-door deliveries | ✅ |
| 3 | Milk purchase from suppliers | ✅ |
| 4 | Supplier payments and statements | ✅ |
| 5 | Customer billing, payments, credit | ✅ |
| 6 | Shop counter sales | ✅ |
| 7 | Production and stock reconciliation | ✅ |
| 8 | Expenses and salary | ✅ |
| 9 | Dashboard and reports | ✅ |
| 10 | Notifications and reminders | ❌ |
| 11 | Frontend app | ❌ |
| 12 | Production readiness | 🟡 |

---

## Phase 0 — Foundation ✅

- ✅ Django/DRF project, PostgreSQL, env-based config
- ✅ Custom `User`, JWT login / refresh / verify / logout (blacklist)
- ✅ OTP password recovery (forgot / reset / change), hashed OTP, throttling, token revocation
- ✅ Multi-tenant `Business` + `BusinessMembership` (OWNER / MANAGER / STAFF)
- ✅ `IsBusinessMember` permission, tenant-scoped querysets, owner protection
- ✅ Soft delete base model
- ✅ Seed command with demo dairies

## Phase 1 — Catalog ✅

- ✅ Product categories, products (CRUD API)
- ✅ Effective-dated product prices with no-overlap DB constraint; `change_product_price`
- ✅ Customers (CRUD API, unique code per business, household/shop/other)

## Phase 2 — Door-to-door deliveries ✅

Client need: delivery boy taps a customer and records litres (cow / buffalo) plus extras, every day.

- ✅ `Delivery` model, price snapshot, check constraints, stock movement on create
- ✅ One delivery per customer / product / date / shift; retry-safe `client_ref`; server-controlled timestamp (staff limited to 48 h back)
- ✅ Detail, cancel (reason, stock reversal) and correct (cancel + replace at original time); deliveries can never be deleted
- ✅ Stock shortfall is recorded and flagged, not blocked
- ✅ Routes: ordered customers per route, standing orders, daily route runs with one visit per customer
- ✅ Outcomes: delivered / not delivered / absent / skipped; extras and "other" with typed price (`allow_open_price`)
- ✅ Suggested basket from standing order, else last delivery (quantities only, never prices)
- ✅ Close (blocked while any visit is pending) with totals; reopen with reason; closed runs block cancel/correct
- ✅ Delivery boy = STAFF login; sees only assigned runs
- ✅ Stock timestamp policy (ARCHITECTURE.md §13) and `stock_net_by_date` report helper
- ❌ Customer-facing confirmation (moved to Phase 10)
- ❌ Standing-order pause/holiday, customer on vacation (not requested yet)

## Phase 3 — Milk purchase from suppliers ✅

Client need: log daily milk from each village supplier with litres + fat; amount auto-calculated; rates differ per supplier.

- ✅ Suppliers CRUD
- ✅ Per-supplier rates by milk type (cow / buffalo), effective-dated
- ✅ `MilkCollection` with rate snapshot and auto amount (litres × fat × rate)
- ✅ Purchase draft → received lifecycle and purchase lines (service level)
- ✅ API: milk collections list / create / detail
- ✅ Cow milk is a flat rate per litre (no fat); buffalo stays litres × fat × rate; `pricing_basis` snapshotted
- ✅ One row per can with shift (morning/evening) and a server-generated label (e.g. `RAM01-261015-M-B2`); retry-safe `client_ref`
- ✅ Daily total: litres bought, total cost, litre-weighted average fat (buffalo), per type and shift; business-wide paginated list with filters
- ✅ Milk entries cancel / correct with audit (never deleted); milk bought posts to stock via per-business product mapping (`milk-config`); negative stock after cancelling used milk is flagged, not blocked
- ✅ API for general purchases under `/api/v1/businesses/<id>/purchases/` (list with filters + pagination, create draft, detail, PATCH draft, `receive/`, `cancel/`); OWNER/MANAGER only; draft cancellation audited (`cancelled_at/by`, reason), no stock or payment effect
- ✅ Corrections to received purchases (Option D confirmed): goods returns (partial/full) and quantity/price mistake corrections via `returns/`, `corrections/`, `adjustments/`. Immutable `PurchaseAdjustment` ledger; original purchase untouched; payable = `total_amount` + adjustments (`SupplierPurchase.net_amount`); stock via `REVERSAL_OUT` / `ADJUSTMENT_IN` movements; statements show `PURCHASE_RETURN` / `PURCHASE_CORRECTION`
- ❌ Physical goods return of milk and refund-due tracking when a paid purchase is returned (shows as negative statement balance only)
- ❌ Fat-based cow milk (not needed now; add `pricing_basis` handling when the client asks)

## Phase 4 — Supplier payments and statements ✅

Client need: pay every ~5 days, cash/online, track advances and what is still owed.

- ✅ Immutable payment ledger, allocations (incl. advances), reversals (service level)
- ✅ Supplier balance selector and account statement
- ✅ API: payments list / create (`client_ref` retry-safe), statement
- ✅ Step 1: payment `client_ref` idempotency, auto-allocation in the service, tenant-scoped lookups, one shared balance function (`get_supplier_position`) for balance and statement, half-open business-timezone day ranges
- ✅ Step 2: partial / full payment reversal (advance first, then allocations newest-first, explicit unwind rows), `payments/<id>/` detail, `reverse/`, `allocate/`, `suppliers/<id>/balance/`
- ✅ Step 3: "pay for period" helper `GET suppliers/<id>/pay-summary/?days=N` (read-only): total outstanding less held advance, with the period's purchases and payments split out
- ✅ Step 4: statement API paginated (`page`, `page_size`; running balance spans the whole period), identity fields (business, supplier, currency, timezone, generated_at), print-friendly HTML `statement/print/` (escaped, business-local times, last 30 days by default, max 366); payments list paginated; statement, print and payments list are now OWNER/MANAGER only
- ⏭ Deferred (not Phase 4): server-side PDF / WhatsApp sharing; payables for non-milk agencies (pending / partial / paid per bill). The existing `SupplierPurchase` + payments already carry non-milk bills; only per-bill status and an agency view are missing. See open questions.

## Phase 5 — Customer billing, payments and credit ✅

Biggest remaining pain: month-end bills took 3–4 days by hand. Design and rules: ARCHITECTURE.md §17.

- ✅ Customer payments (cash / UPI / bank / cheque / other), partial, advance, payment before the bill exists; immutable; reversal (full or partial); `client_ref` retry-safe; server-generated numbers
- ✅ Customer balance: outstanding or advance, plus delivered-but-unbilled reported beside it; paginated account statement
- ✅ Bills: explicit period per bill, live DRAFT, FINALIZE (frozen lines, previous-balance snapshot), VOID with reason and replacement draft
- ✅ A finalized bill closes its period to delivery create / cancel / correct until voided
- ✅ Paying more than the bill (client example: ₹1000 against ₹900) leaves an advance that is applied to the next bill
- ✅ Opening balances (owed = OPENING bill, prepaid = advance payment)
- ✅ Bill PDF (reportlab), deterministic from the stored snapshot
- ✅ `audit_bills` read-only consistency command
- ✅ Hindi bill PDF (`?lang=hi`, "₹"; default per business `bill_language`); Hindi names print correctly on the English bill too. Hindi wording drafted by us: client to confirm
- ❌ Discounts, round-off, write-off of small balances (no rule from the client yet)
- ❌ Delivery boy recording cash collected at the door (OWNER/MANAGER only for now)

## Phase 6 — Shop counter sales ✅

Client need: walk-in and credit sales from the shop, one-tap entry on a tablet.

- ✅ Sale model: customer (optional), items, payment mode (cash / online / credit)
- ✅ Dairy products priced from the product price list at `sold_at` (open-price products take a typed price)
- ✅ "Other items" quick entry with free description + amount (no stock, no per-item tracking)
- ✅ Quick customer search (`customers/?search=`, name / phone / code, max 20) and inline add (`sales/quick-customer/`, code `CUST-<n>`)
- ✅ Credit sales reach the customer ledger through the monthly bill (cash / online never do)
- ✅ Shop credit sales are lines on the same monthly bill as milk deliveries
- ✅ Stock: tracked products post a `SALE` movement; shortfall is flagged on the line, never blocks
- ✅ Cancel / correct (OWNER/MANAGER, reason required), daily summary per payment mode

## Phase 7 — Production and stock reconciliation ✅

Client need: know where each day's milk went (door-to-door, shop, dahi, ghee, ...) and how much ghee/dahi was made.

- ✅ `StockMovement` ledger, stock balance, delivery and purchase-receipt postings, reversal types
- ✅ Production batch: free-form inputs → outputs (`ProductionBatch`, OWNER/MANAGER, cancel / correct with reason; yield shown, never enforced)
- ✅ Daily milk balance: bought / delivered / sold / converted / wastage per cow and buffalo (`/reports/milk-balance/`, OWNER/MANAGER; unposted litres shown)
- ✅ Wastage and manual adjustment API (`StockAdjustment`, OWNER/MANAGER, cancel with reason, shortfall flagged)
- ✅ Month-end closing stock value at owner-set cost price, compared with previous month (`/reports/closing-stock/`, live, OWNER/MANAGER)
- ✅ Stock balance API (`/stock/`, any member, quantities only)

## Phase 8 — Expenses and salary ✅

- ✅ Expense entries with owner-created categories, payee, payment method; cancel / correct with reason (OWNER/MANAGER)
- ✅ Employees with fixed monthly salary (OWNER only)
- ✅ Monthly salary generated by the owner (pro-rata by calendar days for joiners / leavers), bonus and deduction entries, salary payments / advances with a running balance (₹12k due, ₹15k paid → ₹3k advance)
- ✅ `expenses/summary/` (per category) and `salary_cost_by_month` for Phase 9 profit (salary counts in the month it belongs to)
- ❌ Recurring expenses, bill photos, attendance-based deduction, employee ↔ app login link, expense ↔ supplier link (owner chose none of these for now)

## Phase 9 — Dashboard and reports ✅

- ✅ Daily summary: litres bought/sold, purchase cost, sales, collections (cash / online / other), supplier payments, expenses, balances at end of day (`reports/dashboard/daily/`)
- ✅ Weekly / monthly profit estimate: simple and stock-adjusted lines, salary in month buckets for OWNER only (`reports/dashboard/profit/`)
- ✅ Reports: supplier balances + supplier ledger, customer outstanding, stock, product-wise sales, expenses (`reports/...`, OWNER/MANAGER)
- ✅ CSV and PDF export of every report and both dashboard views (`?export=csv|pdf`); PDF supports Hindi
- ❌ Open: weekly view has no salary (salary is monthly; owner has not said how to spread it over weeks); `@extend_schema` docs for the new endpoints

**Owner decisions (Phase 9 planning, 2026-10-18):**
- Build Phase 8 (expenses/salary) BEFORE Phase 9 so profit includes expenses.
- Profit shows two labelled estimates: simple (sales − purchases − expenses) and stock-adjusted. Neither is accounting profit.
- Cash vs online: CASH = cash; UPI, BANK_TRANSFER, CHEQUE = online; OTHER shown separately.
- Sales are recognised on delivery/sale date (accrual), not on bill or payment date.
- "Litres sold" = Cow/Buffalo mapped milk products only.
- Dashboard/reports: OWNER/MANAGER only.
- PDF reports must support Hindi (Devanagari) now; needs a bundled font (Noto Sans Devanagari is on the dev machine, licence/packaging to confirm).
- Limits: list reports 92 days, profit 366 days, PDF 5,000 rows (CSV beyond that).

## Phase 10 — Notifications and reminders ❌

- ❌ Delivery confirmation to customer (SMS preferred; WhatsApp acceptable)
- ❌ Monthly bill sent via WhatsApp/SMS
- ❌ Payment reminders (e.g. on the 10th, repeated every 5 days while unpaid)
- ❌ Supplier balance message after each settlement
- ❌ Broadcast: holiday notice, new product/offer (individual messages, not groups — client dislikes groups)
- ❌ Provider choice and cost check (SMS gateway vs WhatsApp Business API)

## Phase 11 — Frontend app ❌

Client constraints: phone or small tablet at the counter; very simple; delivery boy may use it in the field; customers should not have to install anything.

- 🟡 Decided: Flutter (Android + iOS). Full plan and roadmap: [MOBILE_APP_PLAN.md](MOBILE_APP_PLAN.md) (draft, awaiting approval)
- ❌ Login and business selection
- ❌ Daily delivery entry screen
- ❌ Milk collection entry screen
- ❌ Shop sale screen
- ❌ Payments, bills, dashboard
- ❌ Hindi labels (client works in Hindi)

## Phase 12 — Production readiness 🟡

- ✅ Security hardening for auth; credentials removed from tracked files; `.env.example`
- ✅ ≈190 passing tests
- 🟡 Rotate `SECRET_KEY`, `JWT_SIGNING_KEY`, DB password (old values are in git history)
- ❌ Validate SMTP delivery in staging
- ❌ Redis + `NUM_PROXIES` configured for real traffic
- ❌ API docs (OpenAPI / drf-spectacular)
- ❌ CI pipeline (tests on every push)
- ❌ Deployment (Docker / server), HTTPS, backups, monitoring
- ❌ Pagination / filtering conventions on list endpoints
- ❌ Data import from existing paper records (customers, rates, opening balances)

---

## Next steps (suggested order)

1. **Customer ledger + monthly billing (Phase 5)** — removes the 3–4 day manual task, the client's top problem.
2. ~~Delivery entry (Phase 2)~~ done.
3. ~~Shop sales (Phase 6)~~ done.
4. ~~Daily milk balance and production (Phase 7)~~ done.
5. **Frontend MVP (Phase 11)** in parallel as soon as Phase 2 and 5 APIs are stable.
6. **Notifications (Phase 10)**, then expenses/salary (8) and dashboard (9).

## Open questions for the client

- Staff who work in two shops: an owner cannot issue a reset code for them (account takeover risk). Is "reset by email only" fine, or should a person be able to join a second shop by invite (planned with M11)?
- Staff accounts have no email or phone requirement (username only). Require a phone for staff once OTP login ships?

- Can the delivery boy record cash collected at the door, or only the owner / manager?
- Discounts, round-off, or forgiving small balances: any rule?
- Any required bill / receipt number format (e.g. a printed receipt book)?
- Should the printed bill list each day's quantity, or only totals per product and rate?
- Hindi bill wording (drafted in `apps/billing/pdf.py` LABELS): OK as is?
- Warn at delivery time when a customer owes money?
- Production: any expected yield per product (e.g. kg dahi per 100 L) so low yield can be flagged? Is cream/skim separation recorded as its own batch? (not built; yield is only displayed)
- Wastage: may delivery / shop staff record it, or only owner / manager? (owner / manager now)
- Production security settings: serve over HTTPS only? (redirect, HSTS, secure cookies are not enabled yet; deployment decision)
- Closing stock: freeze a month once closed? Cost price of produced goods (ghee, dahi) is typed by the owner as a PURCHASE price; no costing from milk. (live, not frozen, now)
- Shop sales: split payment (part cash, part credit) on one sale? GST / discount? Printed receipt? (not built; defaults used)

- Supplier statements: is the print-friendly view enough, or is a real PDF / WhatsApp share needed?
- Non-milk agencies: goods suppliers only, or also service / expense bills (transport, repairs)? (Phase 8: expenses use a free-text `payee`, no supplier link.)
- Is cow milk always a flat per-litre rate, or sometimes fat-based too?
- SMS or WhatsApp for bills and reminders (cost and sender setup)?
- One shared tablet at the shop plus delivery boy's phone, or personal phones?
- Do delivery customers pay monthly only, or also daily cash on the route?
- Should customers ever see their own data (view-only link) or only receive messages?
- Hindi-only UI, or English with Hindi labels?
