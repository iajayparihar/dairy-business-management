# Dairy Business Management — working rules for Claude

Read ARCHITECTURE.md and ROADMAP.md first. Django + DRF + PostgreSQL, multi-tenant (Business), UUID keys.

## How to work with the owner (Ajay)
- Plan first: for any new phase give the full detail (models, migrations, services, serializers, views, URLs, logic, tests) and ASK all doubts before writing code. Code only after approval.
- Act as a senior Django architect with a QA mindset: think about concurrency, retries, data integrity, authorization, and tests for each feature.
- Explain unclear business points in plain words and recommend a default. The owner may say "you decide".
- Build step by step; each step ends with green tests. Do not commit or push unless asked.
- Never invent business rules; unknown client answers go to ROADMAP.md "Open questions".
- When the owner repeats a preference or makes a lasting decision, add it to this file.

## Production-grade planning checklist (do this BEFORE presenting any plan)
Past plans needed several revisions because these were found late. Inspect the real code first (models, constraints, services, selectors, migrations, tests), then answer each item in the plan itself:
1. **Reuse existing policy**: find how the codebase already decides the same question (e.g. stock shortfall is recorded and flagged, never blocked; statements already filter `status=RECEIVED`). Do not invent a second rule.
2. **Concurrency**: name every writer of the same rows and the global lock order (purchase -> payment; route run -> delivery -> product). New writers must follow it; add a real threaded/transaction test, not only a sequential one. Postgres FOR UPDATE on a parent row also blocks FK inserts into it: use `no_key=True` for parent rows. Check a design premise ("X must be blocked") against how balances/selectors already treat the case before building it.
3. **Idempotency**: `client_ref` replay compares the full normalized request (including defaulted fields like shift and time); same ref + different data is an error.
4. **Uniqueness**: every generated or supplied identifier has a DB constraint, not only an app check.
5. **Time**: use `Business.timezone`; day ranges are half-open `[start, next_day_start)`; corrections keep the original timestamp and re-resolve the rate at that time.
6. **Inventory**: say what happens to stock when the reversed quantity was already consumed (negative balance is flagged, not hidden). Product mapping validates same business, milk type and unit.
7. **API contract**: say exactly which fields are rejected, ignored or required per case; no "reject/ignore" ambiguity.
8. **Migrations**: backfill without recomputing money; test on a copy of the dev DB; reversible.
9. **Visibility**: if something silently does not happen (e.g. no stock mapping), expose it in the API response.
10. **Audit**: cancel/correct keep reason, who, when; admin is read-only for ledger rows, no delete.
11. **Regression**: run affected app tests after each step and the full suite at the end.
Also: when the owner pastes an external review, verify every claim against the code and say which are already handled, which are valid, and which are wrong.

## Architecture rules
- Money and stock are append-only ledgers: never edit or delete; correct with cancel/reversal/adjustment rows that keep an audit trail (reason, who, when).
- Snapshot prices and rates on the row; later rate changes never recompute old records.
- Logic lives in the service layer (apps/<app>/services.py); views stay thin. Use transaction.atomic and select_for_update on contested rows; DB constraints are the final guard.
- Retry-safe creates use client_ref (unique per business).
- Tenant scoping on every queryset; roles OWNER/MANAGER/STAFF via IsBusinessMember. Purchases and cost data are OWNER/MANAGER only.
- Lists use StandardPagination. Errors go through raise_api_validation_error.
- Tests live in tests/<area>/; the full suite (about 340 tests) must stay green.

## Business decisions already made
- Milk purchase: BUFFALO = litres x fat x rate; COW = litres x flat rate (no fat stored; fat-based cow is a future feature). Rate is per supplier + milk type, effective-dated.
- COW requests that include a `fat` key (even 0) are rejected with 400; BUFFALO requires fat > 0.
- One MilkCollection row per can, with shift (MORNING/EVENING). The user may pick the shift; if omitted it is derived from the request's `collected_at` in the business timezone (before 15:00 = morning).
- Can label / collection_number is server-generated only (client cannot send it): `<SUPPLIER_CODE>-<YYMMDD>-<M|E>-<C|B><seq>`, e.g. RAM01-261015-M-B2. Never reused, even after cancel.
- Milk entries are never deleted: cancel with reason, or Correct (cancel + replacement at the original time/shift). Cancel/Correct is ALLOWED even if a payment is allocated: balances and the allocator ignore allocations on CANCELLED purchases, so the money returns to the supplier's available credit (the earlier "blocked" idea was wrong - verified in apps/payments/selectors.py).
- If cancelling consumed milk makes stock negative: allow and flag the shortfall (same as deliveries), never block.
- Lock order for milk: business (select_for_update(no_key=True)) -> purchase -> collection -> product. Never use a plain FOR UPDATE on the Business row: it blocks every INSERT that references the business and deadlocks against purchase/payment locks (found by the cancel-vs-correct race test).
- Milk bought posts to stock via a per-business Cow/Buffalo product mapping.
- Purchase corrections use Option D (returns and corrections through the immutable PurchaseAdjustment ledger).
- Deliveries can never be deleted; stock timestamp policy is ARCHITECTURE.md section 13.
- Payments: reversals may be partial and are posted only via `reverse_supplier_payment` (advance first, then allocations newest-first through `SupplierPaymentAllocationReversal` rows). `get_supplier_position` is the single place supplier money is summed; outstanding is never clamped. Run data migrations separately from schema migrations (Postgres pending-trigger-events error).
