# Streakly — Habit Tracker with Streaks

Full-stack habit tracker: FastAPI + SQLite backend, vanilla JS + Tailwind
frontend. Users register with an IANA timezone, check in on habits daily
or backfill missed days, and see current/longest streaks computed strictly
against their own local calendar day.

## Stack

- **Backend:** Python, FastAPI
- **Database:** SQLite (4 tables — `users`, `sessions`, `habits`, `checkins`)
- **Auth:** Token-based sessions (bearer token), PBKDF2-SHA256 password hashing (stdlib `hashlib`, 200k iterations, per-user salt)
- **Frontend:** Static HTML + Tailwind (CDN) + vanilla JS, no build step
- **Deploy target:** Railway.app

## Where the local-day logic lives

This is the core of the assignment, so it's isolated deliberately in `main.py`:

- **`local_today(tz_name)`** — the single source of truth for "what day is
  it right now" for a given user. Uses Python's stdlib `zoneinfo` with the
  user's stored IANA timezone string. Every "today" reference in the app
  (validating future dates, resolving a default check-in date, anchoring
  the current streak) goes through this one function.

- **Check-ins are stored as a local calendar day (`YYYY-MM-DD`), not a UTC
  timestamp.** This is the key design decision: if you store a timestamp
  and re-derive "which day is this" every time you read it, you get bugs
  the moment DST shifts or a user is near midnight in their zone. By
  converting to the user's local date once, at write time, and storing
  *that*, streak math afterward becomes plain date-interval arithmetic —
  it never has to touch a timezone again. A `UNIQUE(habit_id, local_date)`
  constraint in SQLite gives duplicate-day protection for free.

- **`compute_streaks(local_dates, tz_name)`** — takes the full set of a
  habit's local check-in dates and derives:
  - `longest_streak`: the longest run of consecutive calendar dates in the
    whole history.
  - `current_streak`: anchored on `local_today()`. If today is checked in,
    count backward from today. If today isn't checked in yet but
    *yesterday* was, the streak is still considered alive (today isn't
    over) — count backward from yesterday instead. Otherwise the streak is 0.

This logic is unit-tested directly (see "Testing" below) against 7 edge
cases: an active streak ending today, an active streak pending today's
check-in, a broken streak, longest ≠ current, no check-ins, a single-day
streak, and a fully stale streak.

## Validation implemented

- Future dates rejected: `422` if `local_date > local_today(tz)`
- Duplicate local-day check-ins rejected: `409` via the DB unique constraint
- Unrecognized IANA timezone strings rejected at registration: `400`
- Password minimum length (8 chars) enforced via Pydantic validator
- Duplicate email registration rejected: `409`
- Wrong credentials on login rejected: `401`
- All habit/check-in routes require a valid bearer token: `401` otherwise
- Ownership checked on every habit/check-in route (a user can't see or check into another user's habit): `404`

## Project structure

```
habit-tracker/
├── main.py             # FastAPI app: auth, habits, timezone/streak logic
├── static/
│   └── index.html        # Frontend (HTML + Tailwind CDN + vanilla JS)
├── requirements.txt
├── Procfile               # Railway start command
├── .env.example
└── .gitignore
```

## Database schema

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    timezone TEXT NOT NULL,       -- IANA string, e.g. 'Asia/Kolkata'
    created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL REFERENCES habits(id),
    local_date TEXT NOT NULL,      -- YYYY-MM-DD, user's local calendar day
    created_at TEXT NOT NULL,
    UNIQUE(habit_id, local_date)
);
```

## API

| Method | Path                          | Notes |
|--------|-------------------------------|-------|
| POST   | `/api/auth/register`           | `{email, password, timezone}` → `{token, email, timezone}` |
| POST   | `/api/auth/login`               | `{email, password}` → `{token, email, timezone}` |
| GET    | `/api/habits`                   | List habits with computed streaks (auth required) |
| POST   | `/api/habits`                   | `{name}` → create a habit |
| GET    | `/api/habits/{id}`               | Habit detail + full check-in history + streaks |
| POST   | `/api/habits/{id}/checkins`      | `{local_date?}` — omit for "today"; include `YYYY-MM-DD` to backfill |

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Open **http://localhost:8000**. The SQLite file (`habits.db`) is created
automatically on first run.

## Testing

The streak logic was unit-tested directly against `compute_streaks()`
with a monkeypatched `local_today()` for deterministic dates, covering:
active streak ending today, active streak pending today, broken streak,
longest ≠ current, empty history, single-day streak, and a stale streak.
The full API was also exercised end-to-end with `curl` — register, bad
timezone rejected, duplicate email rejected, login, wrong password
rejected, unauthenticated request rejected, check-in, duplicate check-in
rejected, future date rejected, backfill, and streak values confirmed
correct after each state change.

## Deploy to Railway

1. Push this repo to GitHub.
2. On [railway.app](https://railway.app): **New Project → Deploy from GitHub repo**.
3. Railway auto-detects Python via `requirements.txt` and starts the app using the `Procfile`.
4. Generate a public domain under **Settings → Networking**.

Note: Railway's filesystem resets on redeploy, so SQLite data doesn't
persist across deploys. Fine for this assignment; production would move
to Postgres (the queries are simple enough that the migration is mostly
swapping the `sqlite3` connection for a Postgres driver).

## What I'd improve with more time

- Move to Postgres with a migration tool (Alembic) instead of `CREATE TABLE IF NOT EXISTS`
- Password reset flow
- Session expiry (currently tokens don't expire)
- Habit archiving/deletion
- A calendar heatmap view of check-in history (GitHub-contributions style)
 Submitted for the Product Engineering intern take-home assignment.
