# Dairy Business Management — Mobile App Plan (v3)

Status: draft for owner review. No code until approved.
Order: **Android first → Android GA → iOS.** One Flutter codebase throughout.
Target: every dairy shop in India (design for 1M+ businesses, 3–5M users).

---

## 1. Context

The backend (Django 5.2 + DRF, PostgreSQL, multi-tenant by `Business`, UUID keys, ~900 green tests) already covers auth, products, customers, suppliers, milk collection, supplier payments, routes / standing orders / route runs, deliveries, billing, shop sales, stock / production, expenses / salary and reports. Every write that matters is retry-safe through `client_ref`.

Nothing exists on the client side yet. This plan covers:
1. What the backend must add or change before a nationwide mobile app can rely on it.
2. The Flutter app from an empty folder to Play Store GA.
3. The iOS port after Android is stable.

Facts checked in the code that shape this plan:
- Login is username / email + password (`apps/users/views.py` `LoginView`); `User.phone_number` exists but is not a login method.
- Businesses are created only in Django admin; there is no self-onboarding API.
- No endpoint tells a logged-in user which businesses they belong to or their role.
- No async worker (Celery / RQ); PDFs and exports render inside the request.
- One Postgres database; offset pagination (`StandardPagination`, page_size 50, max 200).

---

## 2. Scale assumptions (napkin math, to size decisions — not promises)

| Quantity | Assumption | Result |
|----------|-----------|--------|
| Shops | 1,000,000 (long-term target) | |
| Writes per shop per day | ~200 (deliveries, cans, sales, payments) | 200M writes/day ≈ **2,300 writes/s average** |
| Peak factor | Morning 5–9 am + evening collection, ~8× | **~18,000 writes/s peak** |
| Reads | ~5× writes | ~90,000 reads/s peak |
| Rows per year | 200M × 365 | **~73B ledger rows/year** at full scale |
| Devices | Mostly ₹6–12k Android, 2–4 GB RAM, 3G/4G, patchy rural signal | |

Conclusion: a single Postgres primary handles the first **~10–20k shops** comfortably; beyond that the backend must scale in stages (section 4). The mobile app must be designed from day one so it **never needs to change** when the backend scales: stable API contract, tenant id in every URL (already true), idempotent writes (already true), delta sync, and client-side backoff that protects the servers.

---

## 3. Guiding principles

1. **Offline-first for field and counter work.** The network is a bonus, not a requirement, for daily entry.
2. **The server is the source of truth for money.** The app never computes balances, rates or bill totals; it displays server values. Offline it shows "pending".
3. **Every write is idempotent.** Every write carries a device-generated `client_ref`; retries can never duplicate.
4. **Backward-compatible API.** `/api/v1/` never breaks; new fields are additive; old app versions keep working until forced to update.
5. **Protect the backend from the fleet.** Exponential backoff with jitter, no polling loops, honour `Retry-After`, sync batching, gzip.
6. **Low-end first.** Every screen is tested on a 2 GB RAM Android 8 phone before release.
7. **Built for many languages.** Hindi and English first; nothing is hard-coded.
8. **Privacy by design.** Follow India's DPDP Act 2023: minimal PII, encrypted at rest, deletable on request.
9. **Observable.** Every request carries a request id; every crash and slow screen is visible to us before users report it.
10. **iOS-ready, Android-first.** Only plugins that support both platforms; no Android-only shortcuts that block the iOS port.

---

## 4. Backend plan (Django side — required for a nationwide app)

Same working rules as CLAUDE.md: plan per step → owner approval → tests → green suite.

### 4.1 Must-have before the app's first pilot (M0)

| # | Change | Detail |
|---|--------|--------|
| B1 | `GET /api/v1/auth/me/` | User (id, name, phone, email, preferred_language) + active memberships `[{business_id, business_name, role, timezone, bill_language}]`. Own memberships only; soft-deleted / inactive hidden. |
| B2 | `GET /api/v1/app-config/` (public, CDN-cacheable 5 min) | `min_supported_build`, `latest_build`, `maintenance {on, message_en, message_hi}`, `features {flag: bool}`, `support_phone`. Kill switch and force update for the whole fleet. |
| B3 | OpenAPI schema audit | Every view has correct request / response schemas and enums; CI fails on schema warnings; schema diff check in CI flags breaking changes. |
| B4 | Request id + structured logs | `X-Request-ID` (client sends, server echoes / generates), JSON logs, Sentry with the id. |
| B5 | Staging deployment | Managed Postgres (India region), PgBouncer, Redis, gunicorn behind nginx / load balancer with HTTPS, daily backups + restore test, `REDIS_URL`, `NUM_PROXIES`, token purge cron, secret rotation (already listed in ROADMAP Phase 12). |
| B6 | Backend CI | Tests + schema check on every push. |
| B7 | Data import | CSV import (customers, suppliers, milk rates, opening balances) with dry-run report; needed to put real shops on the app. |

### 4.2 Required for Android GA (nationwide launch)

| # | Change | Why |
|---|--------|-----|
| B8 | **Phone + OTP login** (keep password login) | Most shop owners and delivery staff have no email. SMS in India requires **TRAI DLT** registration of sender id and templates (takes weeks — start early). Use an Indian SMS provider (MSG91 / Gupshup / Exotel); Firebase Auth phone OTP is an alternative. OTP: 6 digits, 5 min, 5 attempts, per-phone and per-IP throttles. |
| B9 | **Self-onboarding** (owner creates business + invites staff by phone) | Millions of shops cannot be created in Django admin. *Business rule → open question Q1.* |
| B10 | **Delta sync endpoints** | `GET .../sync/?since=<cursor>` per master list (customers, products + prices, suppliers, routes, standing orders) returning changed rows + tombstones (soft-deleted ids) with a server cursor. At scale, re-downloading full lists every refresh is the single biggest avoidable load. Needs `updated_at` + index on every synced table (customers already has `updated_at`). |
| B11 | **Cursor pagination** for large / growing lists (deliveries, collections, sales, payments, ledgers) | Offset pagination gets slower with depth and returns duplicates / skips while rows are inserted. Keep `StandardPagination` for small lists. |
| B12 | **Batch write endpoint for sync** (`POST .../sync/batch/`, max 50 ops, each with its own `client_ref`, per-op result) | Cuts request count 10–50× during reconnect storms (thousands of phones regaining signal at 7 am). Each op still runs through the existing service layer and lock order; one op failing never rolls back others. |
| B13 | **Async jobs (Celery + Redis)** | PDFs, CSV exports, SMS, emails, push notifications, report precompute. Bill PDFs go to object storage (S3-compatible, India region) and the app downloads them with short-lived signed URLs. |
| B14 | **Push notifications (FCM)** | `POST/DELETE /auth/devices/` (token per login session, removed on logout / session revoke). Used for "bill ready", "sync failed", force-logout, broadcasts. |
| B15 | **Rate limiting per business and per user** (Redis) | One misbehaving shop or buggy build must not hurt others. 429 + `Retry-After`. |
| B16 | **API deprecation policy** | Removing a field needs ≥ 2 app releases + min-build bump; documented in Readme.md. |
| B17 | **DPDP compliance** | Privacy notice + consent at sign-up, account / data deletion request flow (Play Store requires an in-app and web deletion path), data export for the owner, breach-notification runbook, data stored in India. |

### 4.3 Scale stages (triggered by measured load, not built up front)

| Stage | Trigger | Backend work |
|-------|---------|--------------|
| **S1** launch | ≤ 20k shops | Single managed Postgres primary + 1 read replica (reports read from the replica), PgBouncer (transaction mode), Redis, 3+ app servers behind a load balancer, Celery workers, CDN for app-config / static. Multi-AZ. |
| **S2** | p95 DB CPU > 60% or ledger tables > 500M rows | Postgres **partitioning** of ledger tables (deliveries, collections, sales, payments, stock movements) by month; report precompute tables refreshed by Celery; hot-path caching (product prices, business config) in Redis. |
| **S3** | > 200k shops or write peak > 5k/s | **Shard by `business_id`** (Citus or app-level shard router). The schema is already tenant-scoped with UUIDs, so every query already carries the shard key — this is why tenant scoping on every queryset matters. Cross-tenant admin / analytics moves to a separate warehouse (ClickHouse / BigQuery) fed by CDC. |
| **S4** | Business continuity | Warm DR in a second Indian region (Mumbai ↔ Hyderabad), RPO ≤ 5 min, RTO ≤ 1 h, quarterly failover drill. |

The mobile app needs **no change** across S1–S4 (principle 4).

---

## 5. Mobile tech stack (Android first; every choice also supports iOS)

| Area | Choice | Notes |
|------|--------|-------|
| SDK | Flutter stable, pinned with **FVM** (`.fvmrc`), Dart 3 | Same version locally and in CI |
| Lints | `very_good_analysis`, `custom_lint`, `riverpod_lint` | Zero warnings policy |
| State | **Riverpod 2** (code-gen) | Testable, no BuildContext coupling |
| Navigation | **go_router** | Auth + role redirect guards, deep links |
| HTTP | **Dio** | Interceptors: request id, auth, single-flight refresh, retry / backoff, gzip, debug-only logging |
| API client | `openapi_generator` (dart-dio) → `packages/dairy_api` | Generated from `/api/schema/`, pinned generator, never hand-edited |
| Models | freezed + json_serializable | |
| Money | `decimal` | API strings → Decimal; never `double`; `en_IN` grouping (₹1,00,000.00) |
| Local DB | **Drift** on **SQLCipher** | Encrypted cache + outbox; typed migrations |
| Secure storage | flutter_secure_storage | Tokens, DB key (Android Keystore / iOS Keychain) |
| Connectivity | connectivity_plus + API reachability probe | Radio "connected" ≠ internet |
| Background | workmanager (best-effort) | Never the only sync trigger (OEM battery killers) |
| Push | firebase_messaging | FCM on Android; APNs via FCM on iOS later |
| i18n | flutter_localizations + ARB, intl | Indic scripts use **system fonts** (Android ships Noto for all Indic scripts) → no bundled fonts, smaller APK |
| PDF / share | Server PDFs via signed URL, open_filex, share_plus | WhatsApp share of bills |
| App lock | local_auth | PIN / fingerprint |
| Updates | in_app_update (Android) | Flexible update for recommended, immediate for forced |
| Reviews | in_app_review | After N successful days, never mid-task |
| Crash / perf | sentry_flutter | Errors, ANRs, slow frames, release health |
| Analytics | Firebase Analytics (or self-hosted PostHog) | Feature usage, funnels; **no PII**; consent-gated |
| Remote flags | Backend `app-config` (B2) | No third-party dependency for kill switches |
| Images | None in v1 | Product images later via CDN + cached_network_image |
| Tests | flutter_test, mocktail, golden tests (alchemist), integration_test, **Patrol** (native dialogs, permissions), Maestro (smoke) | |
| CI/CD | GitHub Actions + Fastlane | Play tracks; Firebase App Distribution for internal QA |

Plugin rule: a plugin is accepted only if it is maintained (release in last 6 months), supports Android + iOS, and has no closed-source binary. New dependency = review note in the PR.

---

## 6. App architecture

```
dairy_mobile/
  .fvmrc
  melos.yaml                      # monorepo scripts
  packages/
    dairy_api/                    # generated OpenAPI client (do not edit)
    dairy_ui/                     # design system: theme, tokens, widgets
  app/
    lib/
      main_dev.dart  main_staging.dart  main_prod.dart
      bootstrap.dart              # zones, Sentry, error handlers, DI
      app/                        # MaterialApp, router, l10n, app-config gate
      core/
        api/        dio, interceptors, error mapping, server clock offset
        auth/       token store, session controller, single-flight refresh
        db/         drift + sqlcipher, schema versions, migrations
        sync/       outbox, sync engine, delta pull, conflict / failure handling
        money/      Decimal parse / format
        time/       business-timezone day ranges (half-open), shift display
        logging/    ring-buffer logger, redaction, diagnostics export
        analytics/  event API (no PII)
        permissions/ role → capability map
      features/<feature>/
        data/         repository, DTO ↔ domain mapping, local DAO
        domain/       entities, value objects, use cases (only where logic exists)
        presentation/ screens, controllers (Riverpod), widgets
    test/  integration_test/  patrol_test/
  tool/                           # codegen, schema pull, l10n check scripts
```

Features: auth, onboarding, business_select, home / dashboard, customers, suppliers, products, routes, route_runs, deliveries, milk_collection, sales, billing, customer_payments, supplier_payments, purchases, stock, production, expenses, salary, reports, notifications, settings, sync_center, support.

Rules:
- Screen → controller → repository → `dairy_api` / Drift. Screens never touch Dio or SQL.
- **Capability map** (`core/permissions`): role → allowed actions, mirrors backend (STAFF / MANAGER / OWNER; salary OWNER only; purchases and cost data OWNER / MANAGER). The UI hides what the role cannot do; the backend stays the real guard; a 403 refreshes `/me/` (role may have changed).
- **Business context** (`business_id`, role, timezone, bill_language) from `/me/`; switching business clears caches and cancels in-flight requests.
- **Time**: "today" and day filters in `Business.timezone`, never the phone's zone; timestamps ISO 8601 with offset, corrected by server clock offset, never future (backend rejects future).
- Requests send only documented fields (backend rejects unknown fields with 400).
- **Error mapping**: 400 field errors under matching form fields, non-field errors as a banner; 401 → one refresh then login; 403 → "no permission" + refresh role; 404 → "removed"; 409 / 400 business-rule errors → plain-language message; 429 → wait `Retry-After`; 5xx / timeout → retry or queue.

---

## 7. Auth and session

- Login methods: phone + OTP (B8, primary), username / password (existing, fallback).
- After login: `/me/` → 0 businesses: onboarding (owner) or "ask your owner to invite you" (staff) · 1: enter · many: picker (remembers last).
- Tokens: access 30 min, refresh 7 days, rotation on (existing backend).
- **Single-flight refresh** with a mutex. The backend ends all sessions when a rotated refresh token is reused after 10 s, so two refreshes must never run in parallel. Persist the new refresh token to secure storage **before** retrying queued requests.
- Revoked session (logout elsewhere, password change, theft detection, push "force logout") → clear tokens and read cache, **keep the encrypted outbox**, show login with a reason.
- Outbox rows belong to `user_id + business_id`; only that user may send them. Another user logging in on the same phone with a pending queue: blocked until synced, or OWNER signs in to review and discard with reason.
- Logout with pending outbox: blocked with "N entries not synced — Sync now".
- App lock (PIN / fingerprint) on resume after 5 min in background; default on for OWNER, optional for staff.
- Shared counter device mode (if Q4 says yes): quick user switch with per-user PIN, outbox still per user.

---

## 8. Offline-first and sync design

### 8.1 What works offline (v1)
- **Offline writes**: route-run entries, ad-hoc delivery, delivery cancel (with reason), milk collection create, shop sale create (existing customers / walk-in).
- **Online-only writes**: master data (customers, suppliers, products, prices, rates, routes), bills (generate / finalize / void), payments, allocations, reversals, corrections, stock adjustments, production, expenses, salary. These need the server's locks and current balances; queuing them offline would let the phone act on stale money.
- **Offline reads**: products + current sale prices, customers, suppliers, routes, today's run + tasks, standing orders, today's own entries.

### 8.2 Outbox
Row: `id, user_id, business_id, client_ref (UUIDv4), op_type, path, body_json, created_at (server-corrected), status (pending | sending | done | failed | discarded), attempts, next_attempt_at, last_error, server_id, server_number`.

- Written in **one Drift transaction** with the local optimistic row when the user taps Save; the button is disabled during the transaction (no double tap, no crash gap).
- `client_ref` is created once and never changes for that entry; edits after a failure create a **new** entry and `client_ref`.

### 8.3 Sync engine
1. Push before pull: send the outbox, then pull deltas.
2. Uses the batch endpoint (B12) when available, else one request at a time, FIFO per business.
3. A row found in `sending` at startup is resent with the same `client_ref` (the server replays, never duplicates).
4. Network / 5xx / timeout → exponential backoff with **full jitter** (base 2 s, cap 5 min); a fleet-wide outage must not turn into a thundering herd when the server recovers.
5. 429 → wait `Retry-After` for the whole queue.
6. 400 business-rule errors (bill period frozen, run closed, missing rate, same ref with different data, product deactivated) → `failed`, shown in **Sync center** with the server message in the user's language; fix and resend as a new entry, or OWNER / MANAGER discards with a reason (kept in the local audit log and sent in diagnostics).
7. 401 → pause until the same user signs in; 403 → `failed` and refresh role.
8. Server numbers (can label `RAM01-261015-M-B2`, sale numbers) show "pending" until synced.
9. Triggers: after each save, app open / resume, connectivity regained, push "sync", "Sync now" button, best-effort background job. Header shows a sync badge and "last synced hh:mm".

### 8.4 Delta pull (B10)
- One cursor per (business, list), stored in Drift.
- Tombstones remove deleted rows locally.
- Full re-sync if the server answers "cursor too old" or after a DB schema migration.

### 8.5 Clock
- Server time offset from the HTTP `Date` header on every response; entry timestamps = device time + offset.
- If skew > 5 min: banner "phone time is wrong", entries still saved with the corrected time.

---

## 9. Screens by role (mapped to existing endpoints)

**Everyone:** splash / app-config gate, login (OTP / password), forgot / reset / change password, business picker, language, settings, Sync center, notifications inbox, help & support (call / WhatsApp support, send diagnostics), about / licenses, privacy and delete account.

**STAFF — delivery (phone, field, offline)**
- Today's run: `GET / POST route-runs/`, tasks `route-runs/<id>/tasks/`.
- Task list ordered by route sequence, prefilled from standing orders; one tap Delivered / change qty / Skipped → `route-runs/<id>/entries/` (offline).
- Extra delivery `deliveries/`; cancel with reason `deliveries/<id>/cancel/`.
- Close run `route-runs/<id>/close/` (online; refused while outbox has items).
- Only routes assigned to them (backend enforces `assigned_to`).

**STAFF / MANAGER — counter and collection centre (phone or tablet)**
- Milk collection, one row per can: supplier by code → milk type → litres → fat (BUFFALO only; COW sends no `fat` key) → shift auto from time, editable → `suppliers/<id>/milk-collections/`. Today's list, daily summary `milk-collections/daily-summary/`, cancel / correct (manager, online).
- Shop sale: product grid → qty → customer (walk-in / existing / quick customer online) → payment method → `sales/`; summary `sales/summary/`; cancel / correct (manager, online).

**OWNER / MANAGER**
- Home dashboard `reports/dashboard/daily/`.
- Customers: list / search / create / edit, balance, statement, opening balance, standing orders, route assignment.
- Billing: list + filters, generate `bills/generate/`, per-customer bill, detail, finalize, void (reason), PDF + WhatsApp share (`?lang=`).
- Customer payments: record (CASH / UPI / BANK_TRANSFER / CHEQUE / OTHER), allocate, reverse (reason).
- Suppliers: CRUD, milk rates (effective-dated, correct), balance, statement, pay summary, payments / allocate / reverse.
- Purchases (non-milk): create, receive, cancel, returns, corrections, adjustments.
- Products: categories, products, prices `products/<id>/prices/` (never backdated).
- Routes: CRUD, customer order, standing orders, reopen run.
- Stock: balances, adjustments / wastage, production batches.
- Expenses: categories (owner), create / cancel / correct, summary.
- Reports: milk balance, closing stock, profit ("operational estimate"), supplier balances / ledger, customer outstanding, stock, product sales, expenses; CSV / PDF export (async via B13) + share; date pickers enforce 92 / 366-day limits.
- Staff management (with B9): invite by phone, change role, remove.

**OWNER only:** salary (employees, generate month, accruals, bonus / deduction, payments, balances, statements); business settings. Managers see salary figures as "hidden".

---

## 10. UX, localization and accessibility

- **Design before code (M1)**: wireframes for 6 core flows (delivery run, milk can entry, shop sale, bill + share, payment, dashboard), paper-tested with real staff, then Figma and the `dairy_ui` design system.
- Languages: launch **en + hi**. Architecture ready for Marathi, Gujarati, Punjabi, Bengali, Telugu, Tamil, Kannada, Malayalam, Odia: ARB per locale, translators via a TMS (Crowdin / Lokalise), CI fails on missing keys, pseudo-locale build to catch overflow. Server messages (400 errors, PDFs) must also be localized → backend uses Django i18n with the same locale codes. Numbers use Western digits everywhere.
- Big touch targets (≥ 48 dp), numeric keypads, search by code / name / phone, recent items first, last supplier / product remembered, minimal typing.
- Every destructive action asks for a reason and confirms; every submit is double-tap safe.
- Loading (skeleton), empty, error, offline states on every screen; pull-to-refresh; infinite scroll with cursor pages.
- Text scale up to 1.3× without overflow; TalkBack labels; contrast AA; high-contrast outdoor theme; dark mode.
- Layouts: 320–360 dp phones, 8–10" tablets (two-pane at the counter), landscape on tablets only.

---

## 11. Performance and data budgets (enforced in CI / release checks)

| Metric | Budget |
|--------|--------|
| Download size (per-ABI APK from AAB) | ≤ 15 MB |
| Cold start to first usable screen | ≤ 2.5 s on 2 GB RAM Android 8 phone |
| Frame rendering | ≥ 95% frames < 16 ms on lists of 1,000 rows |
| Memory | < 200 MB steady |
| Daily data use (delivery staff) | < 5 MB / day typical |
| Sync after reconnect | 100 queued entries cleared in < 60 s on 3G |
| ANR rate / crash-free | < 0.2% / ≥ 99.7% (Play "bad behavior" thresholds are 0.47% / 1.09%) |

Techniques: R8 shrinking, `--split-per-abi` via AAB, deferred loading of rarely used features (reports, salary), `const` widgets, list virtualization, images none in v1, gzip on API, HTTP/2, isolates for large JSON parsing.

---

## 12. Security

- Tokens and DB key only in Keystore-backed secure storage; Drift encrypted with SQLCipher; `android:allowBackup="false"`, `dataExtractionRules` excludes all.
- Cleartext traffic blocked (`network_security_config`); TLS 1.2+.
- Release builds: `--obfuscate --split-debug-info`, symbols uploaded to Sentry only.
- No secrets in the app (base URL and Sentry DSN only).
- Logs and Sentry scrubbed (tokens, phone, names, amounts); diagnostics export redacted.
- **Play Integrity API** on login and OTP request (block emulators / tampered builds from mass OTP abuse); server verifies the token.
- `FLAG_SECURE` on screens showing salary and OTP.
- Certificate pinning: **not** in v1 (pin rotation can brick millions of installs); revisit with a backup-pin strategy if a threat model requires it.
- Dependency scanning (OSV / Dependabot) in CI; OWASP MASVS L1 checklist before GA; external pentest before GA.

---

## 13. Testing strategy

| Layer | What | Target |
|-------|------|--------|
| Unit | Decimal, timezone day ranges, clock offset, outbox state machine, backoff, single-flight refresh, error mapping, capability map | core ≥ 90% |
| Repository | Mocked Dio with fixtures recorded from the real staging API (incl. 400 shapes) | every endpoint used |
| Widget + golden | Core screens × (en, hi, pseudo-locale) × (phone, tablet) × (light, dark) | all core screens |
| Integration | Against a seeded staging backend: login → run → 20 deliveries offline → kill app → reconnect → exactly 20 server rows | every milestone |
| Patrol E2E | Permissions, notifications, OTP autofill, app lock | release gate |
| Chaos | Airplane toggles mid-send, kill during sync, token expiry during sync, wrong clock, two devices same user, different user with pending queue, server 500s / 429s | release gate |
| Load (backend) | k6 / Locust: 10k simulated devices reconnecting at once against staging; batch sync + delta pull | before GA and each scale stage |
| Device lab | Firebase Test Lab + real devices: Redmi, Realme, Samsung M / A, Vivo, Oppo, one Android Go device, one 8" tablet (Android 8–15) | release gate |
| Contract | CI regenerates `dairy_api` from the staging schema; compile failure = breaking change caught | every PR |

---

## 14. CI/CD and release (Android)

- Branching: trunk-based, short-lived feature branches, PR review mandatory, protected `main`.
- CI on every PR: format, analyze, unit + widget + golden tests, l10n completeness, schema contract, APK size check, dependency scan.
- Builds: flavors `dev` / `staging` / `prod` with separate app ids (`in.<company>.dairy.dev`, `.staging`, prod) and icons.
- Signing: Play App Signing; upload key in CI secrets; keystore backup in two secure places.
- Distribution: Firebase App Distribution (internal QA) → Play **internal** → **closed testing** (pilot shops) → **production staged rollout** 1% → 5% → 20% → 50% → 100% with automatic halt if crash-free < 99.5% or ANR > 0.3%.
- Versioning: semver `x.y.z` + monotonic build number from CI; release notes in en + hi.
- Release cadence after GA: every 2 weeks; hotfix path same day; force update only for security / contract breaks.
- Store compliance: target SDK = Play's current requirement, data safety form, account-deletion URL, privacy policy, content rating, Hindi + English listing, screenshots per language.

---

## 15. Observability and support

- Sentry: crashes, ANRs, slow frames, HTTP errors with request id (matches backend logs), release health dashboards and alerts.
- Analytics events (no PII): app_open, login_success, run_started, delivery_saved, sync_completed (count, duration), sync_failed (reason code), bill_shared, etc. Funnels for onboarding and first delivery.
- Backend: Prometheus / Grafana (p50 / p95 / p99 per endpoint, DB, queue depth), alerting to on-call.
- In-app support: call / WhatsApp support, "Send diagnostics" (redacted logs + device info + outbox summary), FAQ in Hindi.
- Runbooks in Readme.md: release, rollback (halt rollout, raise min build only after a fixed build is live), key rotation, incident response, DPDP breach notification.

---

## 16. Roadmap

Each milestone ends with: tests green in CI, a staging build on Firebase App Distribution, a demo, docs updated.

### Phase A — Android (to GA)

| Milestone | Scope | Est. |
|-----------|-------|------|
| **M0 Backend foundations** | B1 `/me/`, B2 app-config, B3 schema audit, B4 request id + logs, B5 staging, B6 CI. Start DLT registration for SMS now (long lead time). | 2 wk |
| **M1 UX & design system** | 6 core flows, paper test with staff, Figma, `dairy_ui` tokens / components, Hindi label sheet signed off | 2 wk (parallel to M0) |
| **M2 App foundation** | Monorepo, FVM, flavors, bootstrap, generated client, Dio + interceptors, secure storage, Drift + SQLCipher, logging, Sentry, analytics, CI pipeline, size check | 2 wk |
| **M3 Auth & shell** | Password login, `/me/`, picker, capability map, router guards, app-config gate (maintenance / force update), password flows, app lock, settings, language switch | 1.5 wk |
| **M4 Offline engine** | Outbox, sync engine, backoff, clock offset, read cache, Sync center, diagnostics, chaos tests | 2.5 wk |
| **M5 Delivery staff** | Route runs, tasks, offline entries, extra delivery, cancel, close run | 2 wk |
| **M6 Counter** | Milk collection (offline) + summary + cancel / correct; shop sale (offline) + summary + cancel / correct | 2.5 wk |
| **Pilot 1** | B7 data import; 3–5 real shops on closed testing, parallel with paper for 2 weeks; fix list | 2–3 wk |
| **M7 Backend scale pack** | B8 OTP login, B10 delta sync, B11 cursor pagination, B12 batch sync, B13 Celery + object storage, B14 push, B15 rate limits (backend, parallel with M8–M10) | 4 wk (parallel) |
| **M8 Customers & billing** | Customers, standing orders, routes admin, bills (generate / finalize / void / PDF / share), customer payments, statements, opening balance | 2.5 wk |
| **M9 Suppliers, purchases, stock, expenses, salary** | Suppliers, milk rates, supplier payments, statements, purchases, stock, production, products & prices, expenses, salary (owner) | 3 wk |
| **M10 Dashboard, reports, notifications** | Dashboard, all reports, async exports + share, push inbox, OTP login in app, delta sync + batch sync switched on | 2 wk |
| **M11 Onboarding & compliance** | B9 self-onboarding + staff invites (after Q1 decision), B17 DPDP flows, account deletion, privacy policy, Play Integrity | 2 wk |
| **M12 Hardening** | Performance budgets, accessibility, tablet layouts, device lab, load test 10k devices, security review + pentest, translation review | 2 wk |
| **Pilot 2** | 20–50 shops, full app, staged by region | 3 wk |
| **M13 Android GA** | Production (S1) deploy, Play staged rollout, monitoring, on-call, runbooks | 1.5 wk |

**Android GA ≈ 32–36 weeks with 1 Flutter developer + 1 backend developer** (incl. ~20% buffer). A usable field app (deliveries + collection + sales) reaches pilot shops at about **week 14**.

### Phase B — iOS (starts after Android GA is stable for 4 weeks)

| Milestone | Scope | Est. |
|-----------|-------|------|
| **I1 Setup** | Apple Developer account, Mac / Codemagic, bundle ids, signing, provisioning, APNs key in Firebase, iOS flavors | 1 wk |
| **I2 Platform parity** | Build and fix: secure storage (Keychain), SQLCipher, background fetch (BGTaskScheduler limits — sync is foreground-driven anyway), push permission flow, share sheet, file open, biometric (Face ID usage string), keyboard / safe areas, Cupertino back-swipe | 2 wk |
| **I3 iOS QA** | Golden + integration + Patrol on iOS, device lab (iPhone SE 2nd gen → current), performance check | 1.5 wk |
| **I4 App Store** | Privacy nutrition labels, App Tracking (not used → declare none), account deletion, reviewer demo business, TestFlight pilot, App Review (plan 1–2 rounds) | 2 wk |
| **I5 iOS GA** | Phased release (7-day automatic), monitoring | 0.5 wk |

**iOS ≈ 6–8 weeks**, because the business code is shared.

### Phase C — After GA (by demand, measured)
- More languages (section 10), Bluetooth thermal receipt printer, SMS / WhatsApp reminders (ROADMAP Phase 10), UPI payment links / collection (Q7), GST invoicing (Q8), customer view-only link (no app install), scale stages S2–S4, web dashboard for owners.

### Team recommendation
For a nationwide product: at least **2 Flutter, 2 backend, 1 QA, 1 designer (part-time), 1 DevOps / SRE (part-time)** after Pilot 1. With that team, Android GA drops to ~20–24 weeks.

---

## 17. Definition of done (every feature)

- Works offline or shows a clear "needs internet" state.
- en + hi strings complete; no overflow at 1.3× text scale or in the pseudo-locale.
- Role capabilities match backend; 403 handled.
- Loading / empty / error states present.
- Unit + widget tests; golden for new screens; integration test for new write flows.
- Tested on a low-end Android device.
- No new Sentry issues on staging; size budget respected.
- Docs updated (this file, Readme.md for ops changes).

---

## 18. Risks

| Risk | Mitigation |
|------|------------|
| OEM battery killers stop background sync | Foreground-triggered sync, "Sync now", last-synced indicator |
| Reconnect storm at 7 am overloads servers | Batch sync (B12), jittered backoff, rate limits (B15), load test |
| Wrong phone clock | Server time offset + warning |
| SMS DLT approval delays OTP login | Start registration in M0; password login as fallback |
| Staff resist the app | Paper test in M1, Hindi-first UI, parallel paper run, ≤ 3 taps per delivery |
| API change breaks old installs | Schema contract check, additive-only v1, force update via app-config |
| Single DB limit at growth | Staged S1–S4 plan with measured triggers; tenant-scoped schema ready to shard |
| Data import errors corrupt opening balances | Dry-run report + owner sign-off |
| Play policy rejection | Data safety, deletion path, target SDK tracked each release |
| Lost signing key | Play App Signing + two secure backups of the upload key |

---

## 19. Success metrics

- Crash-free users ≥ 99.7%; ANR < 0.2%.
- 0 duplicate rows from sync (server check by `client_ref`).
- Delivery entry ≤ 3 taps; milk can entry ≤ 10 s.
- Month-end billing in < 1 hour (today 3–4 days).
- 95% of queued entries synced within 1 min of network return.
- Day-7 retention of onboarded shops ≥ 60% in pilots.
- API p95 < 300 ms for writes, < 500 ms for lists at S1 load.

---

## 20. Open questions (owner / client decisions — copied to ROADMAP.md "Open questions" when unresolved)

| # | Question | Recommended default |
|---|----------|---------------------|
| Q1 | Self-onboarding: can any shop owner sign up from the app, or do we approve / create each business? | Self sign-up with phone OTP, business active immediately |
| Q2 | Pricing: free, subscription, or per-shop plan? (affects onboarding, limits, Play billing rules) | Decide before M11; free during pilots |
| Q3 | Phone OTP as the main login? | Yes; keep password as fallback |
| Q4 | Shared counter tablet with several staff, or personal phones? | Personal phones; shared-device mode later |
| Q5 | Can the delivery boy collect cash at the door? | Not in v1 (ROADMAP open question) |
| Q6 | Languages after Hindi / English, and in what order? | Marathi, Gujarati, Punjabi first (dairy belt) |
| Q7 | UPI collection in the app (payment links / QR)? | Phase C |
| Q8 | GST invoices needed? | Phase C |
| Q9 | Bluetooth receipt printer — which models do shops use? | Phase C; WhatsApp PDF share meanwhile |
| Q10 | Who owns the Play / Apple developer accounts and the brand (name, logo, colours)? | Company account, not personal |
| Q11 | Production cloud provider (AWS / GCP / Azure India regions)? | AWS Mumbai + Hyderabad |
| Q12 | Who may discard a failed offline entry? | OWNER / MANAGER, with reason |

---

## 21. Next step

After approval of this file: plan **M0 / B1 `GET /api/v1/auth/me/`** in full detail per CLAUDE.md (models touched, serializer, view, URL, permissions, tests), then build it.
