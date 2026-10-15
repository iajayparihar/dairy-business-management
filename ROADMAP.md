# Roadmap

What we are building: a mobile-friendly dairy management system that replaces the paper register (milk purchase, door-to-door supply, shop sales, credit, supplier payments, stock, expenses). Architecture is in [ARCHITECTURE.md](ARCHITECTURE.md).

Legend: ✅ done · 🟡 partial · ❌ not started

## Summary

| Phase | Theme | Status |
|---|---|---|
| 0 | Foundation (tenancy, auth, roles) | ✅ |
| 1 | Catalog: products, prices, customers | ✅ |
| 2 | Door-to-door deliveries | ✅ |
| 3 | Milk purchase from suppliers | 🟡 |
| 4 | Supplier payments and statements | 🟡 |
| 5 | Customer billing, payments, credit | ❌ |
| 6 | Shop counter sales | ❌ |
| 7 | Production and stock reconciliation | 🟡 |
| 8 | Expenses and salary | ❌ |
| 9 | Dashboard and reports | ❌ |
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

## Phase 3 — Milk purchase from suppliers 🟡

Client need: log daily milk from each village supplier with litres + fat; amount auto-calculated; rates differ per supplier.

- ✅ Suppliers CRUD
- ✅ Per-supplier rates by milk type (cow / buffalo), effective-dated
- ✅ `MilkCollection` with rate snapshot and auto amount (litres × fat × rate)
- ✅ Purchase draft → received lifecycle and purchase lines (service level)
- ✅ API: milk collections list / create / detail
- ❌ Fixed-rate-per-litre mode for cow milk (client uses e.g. ₹37–40 flat; verify current calculation supports it)
- ❌ Multiple cans per supplier per day, shift (morning/evening)
- ❌ Daily total: litres bought, total cost, average fat
- ❌ API for general purchases (raw material such as cream, shop goods from agencies like "Suvidha") — create, receive, cancel

## Phase 4 — Supplier payments and statements 🟡

Client need: pay every ~5 days, cash/online, track advances and what is still owed.

- ✅ Immutable payment ledger, allocations (incl. advances), reversals (service level)
- ✅ Supplier balance selector and account statement
- ✅ API: payments list / create, statement
- ❌ API for reversing a payment and for allocating an advance
- ❌ "Pay for period" helper: compute amount due for last N days and prefill payment
- ❌ Printable / shareable statement for the supplier
- ❌ Payables for non-milk agencies (pending vs paid per bill)

## Phase 5 — Customer billing, payments and credit ❌

Biggest remaining pain: month-end bills take 3–4 days by hand.

- ❌ Customer payment ledger (cash / UPI / online, advance, partial) mirroring supplier payments
- ❌ Customer balance: outstanding (udhaar) or advance
- ❌ Monthly bill generation: per-customer summary (cow litres, buffalo litres, extras, rates, total, paid, due)
- ❌ Bill PDF (hard copy) for the ~5% without WhatsApp
- ❌ Bill lock / finalize so a sent bill cannot silently change
- ❌ Handling "paid in advance, bill not yet seen" cases (client example: ₹1000 paid against ₹900 bill)

## Phase 6 — Shop counter sales ❌

Client need: walk-in and credit sales from the shop, one-tap entry on a tablet.

- ❌ Sale model: customer (optional), items, payment mode (cash / online / credit)
- ❌ Dairy products priced from product price list (paneer 250 g at ₹400/kg etc.)
- ❌ "Other items" quick entry with free amount (no per-item tracking, by client's choice)
- ❌ Quick customer search by name; add new customer inline
- ❌ Credit sales feed the customer ledger from Phase 5
- ❌ Include shop purchases in the same monthly bill as milk deliveries

## Phase 7 — Production and stock reconciliation 🟡

Client need: know where each day's milk went (door-to-door, shop, dahi, ghee, ...) and how much ghee/dahi was made.

- ✅ `StockMovement` ledger, stock balance, delivery and purchase-receipt postings, reversal types
- ❌ Production batch: input (milk / cream) → output (ghee kg, dahi kg, paneer, mawa)
- ❌ Daily milk balance: bought vs delivered vs sold vs converted vs wastage
- ❌ Wastage and manual adjustment API
- ❌ Month-end closing stock value; compare with previous month (client's stock-gain/loss view)
- ❌ Stock balance API

## Phase 8 — Expenses and salary ❌

- ❌ Expense entries (electricity ≈ ₹15k/month, rent, transport, etc.) with category
- ❌ Employees with fixed monthly salary
- ❌ Salary payments / advances with running balance (e.g. ₹12k due, ₹15k paid → ₹3k advance)

## Phase 9 — Dashboard and reports ❌

- ❌ Daily summary: litres bought/sold, purchase cost, sales, collections, cash vs online
- ❌ Weekly / monthly profit view (sales − purchase − expenses)
- ❌ Reports: supplier ledger, customer outstanding, stock, expense, product-wise sales
- ❌ Export (CSV/PDF)

## Phase 10 — Notifications and reminders ❌

- ❌ Delivery confirmation to customer (SMS preferred; WhatsApp acceptable)
- ❌ Monthly bill sent via WhatsApp/SMS
- ❌ Payment reminders (e.g. on the 10th, repeated every 5 days while unpaid)
- ❌ Supplier balance message after each settlement
- ❌ Broadcast: holiday notice, new product/offer (individual messages, not groups — client dislikes groups)
- ❌ Provider choice and cost check (SMS gateway vs WhatsApp Business API)

## Phase 11 — Frontend app ❌

Client constraints: phone or small tablet at the counter; very simple; delivery boy may use it in the field; customers should not have to install anything.

- ❌ Decide: PWA vs native vs React Native
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
3. **Shop sales (Phase 6)** — so credit from the counter lands in the same bill.
4. **Daily milk balance and production (Phase 7)**.
5. **Frontend MVP (Phase 11)** in parallel as soon as Phase 2 and 5 APIs are stable.
6. **Notifications (Phase 10)**, then expenses/salary (8) and dashboard (9).

## Open questions for the client

- Is cow milk always a flat per-litre rate, or sometimes fat-based too?
- SMS or WhatsApp for bills and reminders (cost and sender setup)?
- One shared tablet at the shop plus delivery boy's phone, or personal phones?
- Do delivery customers pay monthly only, or also daily cash on the route?
- Should customers ever see their own data (view-only link) or only receive messages?
- Hindi-only UI, or English with Hindi labels?
