# Dairy Business Management — Development Setup

This guide explains how to prepare the Python environment, install PostgreSQL, create the application database and user, configure Django, and run database migrations on Ubuntu/Linux.

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

---

**Development note:** Use separate credentials and environment configuration for development, testing, and production. Back up important data before making database or migration changes.


___

# Test Cmd
## run all of the tests

python manage.py test 

## run specific tests so change the module name
python manage.py test tests.payment


python manage.py test tests.payment --verbosity 2
## Password recovery (OTP)

Endpoints under `/api/v1/auth/`: `password/forgot/`, `password/reset/`, `password/change/` (login/refresh/verify/logout unchanged).

- **OTP**: 6 digits, stored only as an HMAC hash, single use, TTL `PASSWORD_RESET_OTP_TTL_MINUTES` (default 10), 5 attempts, 60 s resend cooldown. Enforced in PostgreSQL (row lock on the user + partial unique index "one live OTP per user"), independent of the cache.
- **Tokens**: reset and change revoke *all* the user's refresh tokens (all-devices logout). Already-issued **access tokens stay valid until expiry (30 min)**; immediate access-token revocation is not implemented (accepted trade-off; shorten `ACCESS_TOKEN_LIFETIME` if needed). Session logins are invalidated automatically by Django's password hash.
- **Throttles** (per client IP / user): login 10/min, forgot 5/hour, reset 10/hour, change 10/hour. Counters live in the Django cache; the default locmem cache is per-process, so set `REDIS_URL` (and `pip install redis`) for multi-worker deployments, and `NUM_PROXIES` behind a reverse proxy. Throttling alone does not stop distributed abuse; the DB limits above are the real guard.
- **Email**: `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD/USE_TLS/USE_SSL/TIMEOUT`, `DEFAULT_FROM_EMAIL`. Console backend is the default only when `DEBUG=True`; otherwise SMTP. Accounts with no email must be reset by an admin (`manage.py changepassword`).
- **Deploy**: `python manage.py migrate`, then schedule `python manage.py purge_password_otps` (daily; deletes expired/used OTPs older than 1 day; idempotent).
- **Secrets**: copy `.env.example` to `.env`. `.env.local` was previously committed and is now untracked; it remains in Git history, so rotate `SECRET_KEY`, `JWT_SIGNING_KEY` and the DB password if they were ever used outside local dev.
