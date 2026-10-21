# Dairy Business Management — Development Setup

This guide explains how to prepare the Python environment, install PostgreSQL, create the application database and user, configure Django, and run database migrations on Ubuntu/Linux.

## About the project

A multi-tenant backend for dairy businesses. It replaces the paper register of a dairy shop with door-to-door supply: milk bought from village suppliers, milk delivered to customers, shop counter sales, dairy products, credit (udhaar), supplier and customer payments, stock and production, expenses and salary, and reports.

| | |
|---|---|
| Stack | Python 3.12, Django 5.2, Django REST Framework 3.18, SimpleJWT (with token blacklist), PostgreSQL (`psycopg` 3), `django-environ`, `drf-spectacular` (Swagger), `reportlab` + Pillow (PDF) |
| Tenancy | One database, many businesses. Every row belongs to a `Business`; every query is scoped to it. Keys are UUIDs. |
| Roles | `OWNER`, `MANAGER`, `STAFF` per business (see [Roles and access](#roles-and-access)). |
| Money and stock | Append-only ledgers: rows are never edited or deleted, mistakes are fixed with cancel / reversal / adjustment rows that keep a reason, who and when. |
| Locale defaults | Timezone `Asia/Kolkata`, currency `INR`, bills in English or Hindi. |

Related documents:

- [ARCHITECTURE.md](ARCHITECTURE.md) — layers, data model, lock order and design decisions.
- [ROADMAP.md](ROADMAP.md) — phases, status and open questions.
- [CLAUDE.md](CLAUDE.md) — working rules and the business decisions already made.

**Already set up?** Jump to [Load demo data](#load-demo-data-optional-development-only), then [Explore the API (Swagger)](#explore-the-api-swagger).

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Create and activate the Python virtual environment](#2-create-and-activate-the-python-virtual-environment)
3. [Install and start PostgreSQL](#3-install-and-start-postgresql)
4. [Create the database user and database](#4-create-the-database-user-and-database)
5. [Test the database connection](#5-test-the-database-connection)
6. [Configure Django environment variables](#6-configure-django-environment-variables)
7. [Run Django migrations](#7-run-django-migrations) (includes [demo data](#load-demo-data-optional-development-only))
8. [Create a Django administrator](#8-create-a-django-administrator-optional)
9. [Start the development server](#9-start-the-development-server)
   - [Explore the API (Swagger)](#explore-the-api-swagger)
   - [Roles and access](#roles-and-access)
10. [Troubleshooting](#10-troubleshooting)
11. [Quick setup checklist](#11-quick-setup-checklist)
12. [Tests](#test-cmd), [password recovery](#password-recovery-otp), [management commands](#management-commands)

## 1. Prerequisites

Ensure that Python 3, `venv`, `pip`, and Git are available.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

From the project root, confirm that the expected files are present:

```bash
ls
```

You should see `manage.py` and `requirements.txt`.

## 2. Create and activate the Python virtual environment

Run these commands from the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

> **Note:** The environment directory is named `.venv`, so activate it with `source .venv/bin/activate` (not `source venv/bin/activate`).

Upgrade `pip` and install the project dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

To leave the virtual environment later, run:

```bash
deactivate
```

## 3. Install and start PostgreSQL

Install PostgreSQL and its additional contributed utilities:

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
```

Check the service:

```bash
sudo systemctl status postgresql
```

If it is not running, start it and enable it at boot:

```bash
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

## 4. Create the database user and database

The examples below use these values:

| Setting | Value |
|---|---|
| Database | `dairy_db` |
| Database user | `dairy_user` |
| Database host | `localhost` |
| Database port | `5432` |

Replace `your_password` with a strong password of your own. Do not commit real passwords to Git.

Open the PostgreSQL shell as the PostgreSQL administrator:

```bash
sudo -u postgres psql
```

Run the following SQL commands inside `psql`:

```sql
CREATE USER dairy_user WITH PASSWORD 'your_password';
CREATE DATABASE dairy_db OWNER dairy_user;
GRANT ALL PRIVILEGES ON DATABASE dairy_db TO dairy_user;
```

If the application user must create databases for local development, you may optionally grant that ability:

```sql
ALTER USER dairy_user CREATEDB;
```

**Security note:** `CREATEDB` is not normally required for Django migrations or day-to-day application use. Grant it only if your development workflow needs it. Avoid granting superuser privileges to the application user.

Exit the PostgreSQL shell:

```sql
\q
```

> If `dairy_user` or `dairy_db` already exists, PostgreSQL will report an error for the corresponding `CREATE` command. Check the existing objects before deciding whether to reuse or alter them; do not drop them blindly.

## 5. Test the database connection

Connect using the application credentials:

```bash
psql -h localhost -p 5432 -U dairy_user -d dairy_db
```

Enter the password when prompted. If the connection succeeds, the prompt should resemble:

```text
dairy_db=>
```

Exit with:

```sql
\q
```

If `psql` is not found, install the PostgreSQL client package:

```bash
sudo apt install -y postgresql-client
```

## 6. Configure Django environment variables

Create or update the `.env` file in the project root, using the variable names expected by your Django settings:

```dotenv
DB_NAME=dairy_db
DB_USER=dairy_user
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432
```

Important:
- Use the same variable names that your Django settings or configuration library reads.
- Keep `.env` out of version control. Add it to `.gitignore`.
- Commit a `.env.example` file with placeholder values instead of real credentials.
- Do not paste production passwords into source code or commit history.
- A variable exported in your shell **wins over `.env`** (`django-environ` does not override existing environment variables). Run `env | grep DB_` to be sure which database Django will use; see [Troubleshooting](#commands-run-against-the-wrong-database).
- Keep the database name in `.env` in step with the database you created in Section 4. This guide's examples use `dairy_db`; a second, empty database for demo data (for example `dairy_db01`) works the same way.

Example `.gitignore` entry:

```gitignore
.env
.venv/
__pycache__/
*.py[cod]
```

## 7. Run Django migrations

Ensure the virtual environment is active and run commands from the directory containing `manage.py`:

```bash
python manage.py check
python manage.py migrate
```

If your project contains model changes that require new migrations, create them first:

```bash
python manage.py makemigrations
python manage.py migrate
```

Only create migrations when you have made model changes that need to be recorded. Review generated migration files before committing them.

### Load demo data (optional, development only)

One command loads a realistic multi-tenant dataset (3 dairies, owners / managers / staff,
~2 months of milk collections, route deliveries, shop sales, production, bills, payments,
expenses and salary) through the real service layer:

```bash
python manage.py seed_demo           # empty database
python manage.py seed_demo --fresh   # WIPES the whole database first, then seeds
```

It runs only with `DEBUG=True` and takes about 3 minutes. Every login uses the password
`Dairy@12345`; usernames read `<name>_<role>_<business>` (e.g. `ajay_owner_abhigyan`,
`suresh_manager_abhigyan`, `raju_staff_abhigyan`) and the full list is printed at the end.
Each area has its own module in `apps/businesses/seed/`.

Before you seed, check which database you are about to write to. The command prints
`Database: <name> on <host>:<port>` first and stops with "Run migrate first" if migrations are
pending. `--fresh` deletes **every row in that database**, including your own users, so run it
only on a database that holds nothing you want to keep.

What gets created (history runs from the 1st of last month to today):

| Business | Size | Bill language | Notes |
|---|---|---|---|
| Abhigyan Dairy (`abhigyan-dairy`) | large: 20 customers, 6 farmers, 3 routes | English | two owners, one manager, three staff |
| Vij Dairy (`vij-dairy`) | medium: 14 customers, 4 farmers, 2 routes | Hindi | one owner, one manager, two staff |
| Gopal Dairy (`gopal-dairy`) | small: 8 customers, 3 farmers, 1 route | English | one owner, one staff |
| Shree Krishna Dairy (closed) | none | - | an inactive business, to test refusals |

Logins (password for all: `Dairy@12345`; the pattern is `<name>_<role>_<business>`):

| Username | Role | Business |
|---|---|---|
| `admin` | Django superuser (admin site) | - |
| `ajay_owner_abhigyan`, `priya_owner_abhigyan` | OWNER | Abhigyan |
| `suresh_manager_abhigyan` | MANAGER | Abhigyan |
| `raju_staff_abhigyan`, `mohan_staff_abhigyan`, `deepak_staff_abhigyan` | STAFF | Abhigyan |
| `vikram_owner_vij` | OWNER | Vij |
| `kavita_manager_vij` | MANAGER | Vij |
| `sonu_staff_vij`, `pappu_staff_vij` | STAFF | Vij |
| `gopal_owner_gopal` | OWNER | Gopal |
| `ramu_staff_gopal` | STAFF | Gopal |
| `neha_manager_abhigyan_staff_vij` | MANAGER in Abhigyan **and** STAFF in Vij | two businesses |
| `vikas_staff_abhigyan_inactive` | STAFF with an inactive membership | Abhigyan |
| `kiran_no_business` | logged-in user with no business | - |
| `shyam_owner_krishna_closed` | OWNER of an inactive business | Krishna |

Good starting points for testing: today's morning route run is left open with half of its
visits pending (log in as `raju_staff_abhigyan` to finish it); last month's bills are finalized
and partly paid, with one voided bill and one reversed payment per business; stock can be slightly
short in some products to show flagged shortfalls. Business IDs change on every seed; find
them in the Django admin under Businesses, or with:

```bash
python manage.py shell -c "from apps.businesses.models import Business; print(*Business.objects.values_list('name', 'id'), sep='\\n')"
```

## 8. Create a Django administrator (optional)

To access the Django admin site, create a superuser:

```bash
python manage.py createsuperuser
```

Follow the prompts to set the administrator username, email, and password.

## 9. Start the development server

```bash
python manage.py runserver
```

By default, Django serves the development site at `http://127.0.0.1:8000/`.

Use Django's development server for local development only, not as the production deployment server.

### Explore the API (Swagger)

With `DEBUG=True` the interactive docs are public; in production they need an admin session.

| URL | What |
|---|---|
| `http://127.0.0.1:8000/api/docs/` | Swagger UI |
| `http://127.0.0.1:8000/api/schema/` | OpenAPI schema (YAML) |
| `http://127.0.0.1:8000/admin/` | Django admin |

Endpoints are grouped by area (Auth, Products & Prices, Customers, Suppliers & Milk Rates, Milk
Collection, Purchases, Supplier Payments, Routes & Deliveries, Shop Sales, Customer Bills,
Customer Payments, Stock & Production, Expenses, Employees & Salary, Reports & Dashboard). The
grouping rules live in `config/swagger.py`.

To call a protected endpoint:

1. Open **1. Auth** and run `POST /api/v1/auth/token/` with `{"username": "...", "password": "..."}`.
2. Copy the `access` value from the response.
3. Click **Authorize** (top right), paste the token, and confirm. The token is kept after a page refresh.
4. Replace `business_id` in the path with a business ID the user belongs to.

Access tokens last 30 minutes and refresh tokens 7 days (`POST /api/v1/auth/token/refresh/`).
A user who is not a member of a business gets `403` for that business's URLs, which is also a
quick tenant-isolation check. Business IDs are listed in [Load demo data](#load-demo-data-optional-development-only).

### Roles and access

Each user has a role per business. The main rules:

| Area | OWNER | MANAGER | STAFF |
|---|---|---|---|
| Read products, categories, customers, suppliers (master data) | yes | yes | yes |
| Create / edit products, customers, suppliers, selling prices | yes | yes | no |
| Record a delivery or a shop sale | yes | yes | yes (last 48 hours only) |
| Cancel / correct a delivery or a shop sale | yes | yes | no |
| Create routes and standing orders; work any route run | yes | yes | no |
| Work a route run | yes | yes | only runs assigned to them |
| Read stock balances (quantities) | yes | yes | yes |
| Milk collection, purchases, supplier payments, supplier milk rates, purchase (cost) prices | yes | yes | no |
| Customer bills and customer payments | yes | yes | no |
| Wastage / stock adjustments, production batches, closing stock and milk balance | yes | yes | no |
| Expenses and expense categories | yes | yes | no |
| Reports and dashboards | yes | yes | no |
| Salary figures (employees, accruals, payments, balances) | yes | **no** | no |

Django superusers (the `admin` account) can open any active business. A user with no active
membership in a business gets `403` for that business's URLs.

Nobody can record a delivery, sale, payment or expense in the future. See `CLAUDE.md` and
`ARCHITECTURE.md` for the exact rules and the reasons behind them.

## 10. Troubleshooting

### PostgreSQL service is not running

```bash
sudo systemctl start postgresql
sudo systemctl status postgresql
```

### Password authentication fails

- Confirm that the password in `.env` matches the PostgreSQL user's password.
- Check `DB_HOST`, `DB_PORT`, `DB_NAME`, and `DB_USER`.
- Test the connection directly with the `psql` command in Section 5.

### Django cannot connect to PostgreSQL

- Confirm PostgreSQL is running.
- Confirm the database and user exist.
- Confirm the Python PostgreSQL driver required by `requirements.txt` is installed.
- Confirm Django is loading the `.env` file and reading the expected variable names.

### `ModuleNotFoundError` or missing dependencies

Activate the environment and reinstall the dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Virtual environment activation fails

From the project root, check that `.venv` exists:

```bash
ls -la .venv
```

If it does not exist, create it again using Section 2.

### Commands run against the wrong database

Symptom: `migrate` or `seed_demo` reports data you did not expect, or a freshly created database stays empty.

Cause: `DB_NAME` (or another `DB_*` variable) is exported in your shell, in `~/.zshrc`, `~/.bashrc`, `~/.profile` or your IDE's environment. An exported variable overrides `.env`.

```bash
env | grep DB_     # what Django will actually use
unset DB_NAME      # fall back to the value in .env
```

To use another database for a single command without touching the environment:

```bash
DB_NAME=dairy_db01 python manage.py migrate
```

### `relation "..." does not exist`

The tables have not been created in the database Django is connected to. Run `python manage.py migrate`, then check `python manage.py showmigrations` shows `[X]` for every migration.

### `seed_demo` says "Demo data is already loaded"

The database already contains the Abhigyan demo business. Use `python manage.py seed_demo --fresh` **only** if you are sure the whole database may be wiped.

### `seed_demo` says "runs only with DEBUG=True"

Set `DEBUG=True` in `.env` (development only). The command is blocked on purpose so it cannot run against production data.

```bash
sudo -u postgres psql
CREATE USER dairy_user WITH PASSWORD 'YOUR_STRONG_PASSWORD';

CREATE DATABASE dairy_db OWNER dairy_user;

GRANT ALL PRIVILEGES ON DATABASE dairy_db TO dairy_user;

\c dairy_db

GRANT ALL ON SCHEMA public TO dairy_user;

ALTER SCHEMA public OWNER TO dairy_user;

ALTER USER dairy_user CREATEDB;
\q
```

## 11. Quick setup checklist

- [ ] Python and PostgreSQL are installed.
- [ ] The `.venv` virtual environment is active.
- [ ] Dependencies from `requirements.txt` are installed.
- [ ] PostgreSQL is running and enabled at boot.
- [ ] `dairy_user` and `dairy_db` exist.
- [ ] The `psql` connection test succeeds.
- [ ] `.env` contains the correct database settings and is not committed.
- [ ] `python manage.py check` succeeds.
- [ ] `python manage.py migrate` succeeds.
- [ ] `env | grep DB_` shows the database you intend to use (no stray `DB_NAME` override).
- [ ] (Optional) `python manage.py seed_demo` loads the demo data.
- [ ] `http://127.0.0.1:8000/api/docs/` opens and a login at `POST /api/v1/auth/token/` returns an `access` token.

---

**Development note:** Use separate credentials and environment configuration for development, testing, and production. Back up important data before making database or migration changes.

## Test Cmd

## run all of the tests

python manage.py test

## run specific tests so change the module name

python manage.py test tests.payment

python manage.py test tests.payment --verbosity 2

Test folders are in `tests/<area>/`: `billing`, `business`, `common`, `customer`, `delivery`, `expenses`, `inventory`, `payment`, `products`, `purchases`, `reports`, `routes`, `sales`, `supplier`, `users`.

```bash
python manage.py test tests.billing                 # one area
python manage.py test tests.users.tests.SomeClass   # one class (dotted path)
python manage.py test --parallel 4                  # whole suite on 4 processes
```

The suite creates and destroys its own `test_<DB_NAME>` database, so your data is not touched, but the database user needs the `CREATEDB` privilege (see Section 4). The full suite has about 940 tests and takes roughly 20 minutes; run the area you changed first and the full suite before committing.

## Password recovery (OTP)

## unset DB_NAME

Run this if `env | grep DB_` shows a `DB_NAME` you did not intend (see [Troubleshooting](#commands-run-against-the-wrong-database)).

## seeding

python manage.py seed_demo --fresh

Wipes the whole database, then loads the demo data (details and logins in [Load demo data](#load-demo-data-optional-development-only)). Confirm the `Database:` line it prints first.

Endpoints under `/api/v1/auth/`: `password/forgot/`, `password/reset/`, `password/change/` (login/refresh/verify/logout unchanged).

- **OTP**: 6 digits, stored only as an HMAC hash, single use, TTL `PASSWORD_RESET_OTP_TTL_MINUTES` (default 10), 5 attempts, 60 s resend cooldown. Enforced in PostgreSQL (row lock on the user + partial unique index "one live OTP per user"), independent of the cache.
- **Tokens**: reset and change revoke *all* the user's refresh tokens (all-devices logout). Already-issued **access tokens stay valid until expiry (30 min)**; immediate access-token revocation is not implemented (accepted trade-off; shorten `ACCESS_TOKEN_LIFETIME` if needed). Session logins are invalidated automatically by Django's password hash.
- **Throttles** (per client IP / user): login 10/min, forgot 5/hour, reset 10/hour, change 10/hour. Counters live in the Django cache; the default locmem cache is per-process, so set `REDIS_URL` (and `pip install redis`) for multi-worker deployments, and `NUM_PROXIES` behind a reverse proxy. Throttling alone does not stop distributed abuse; the DB limits above are the real guard.
- **Email**: `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD/USE_TLS/USE_SSL/TIMEOUT`, `DEFAULT_FROM_EMAIL`. Console backend is the default only when `DEBUG=True`; otherwise SMTP. Accounts with no email must be reset by an admin (`manage.py changepassword`).
- **Deploy**: `python manage.py migrate`, then schedule `python manage.py purge_password_otps` (daily; deletes expired/used OTPs older than 1 day; idempotent).
- **Secrets**: copy `.env.example` to `.env`. `.env.local` was previously committed and is now untracked; it remains in Git history, so rotate `SECRET_KEY`, `JWT_SIGNING_KEY` and the DB password if they were ever used outside local dev.

## Management commands

| Command | Purpose |
|---|---|
| `python manage.py seed_demo [--fresh]` | Load multi-tenant demo data (development only). |
| `python manage.py audit_bills` | Read-only check of customer bills, allocations and balances. Exit status 1 means a promise is broken. Run after deploys and migrations, or when a balance looks wrong. |
| `python manage.py audit_deliveries` | Read-only check for duplicate deliveries; run **before** applying `deliveries.0004`. |
| `python manage.py purge_password_otps` | Delete expired or used password-reset OTPs older than one day. Safe to rerun; schedule it daily. |
| `python manage.py spectacular --file schema.yml` | Write the OpenAPI schema to a file. |
