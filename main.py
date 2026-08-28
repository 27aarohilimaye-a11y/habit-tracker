"""
Habit Tracker with Streaks — FastAPI backend.

Where the local-day logic lives
--------------------------------
All timezone-sensitive logic is centralized in two functions:

    - `local_today(tz_name)`  — returns "what day is it right now, for this
      user's IANA timezone", as a date object. This is the single source of
      truth for "today" anywhere in the app (validation, dashboard, etc).
    - `compute_streaks(local_dates, tz_name)` — takes the set of local
      calendar dates a habit has been checked into and derives current +
      longest streak, using `local_today` as the anchor for "current".

Check-ins are stored as a `local_date` (YYYY-MM-DD) string — the *user's*
calendar day, not a UTC timestamp — because storing timestamps and
re-deriving the day at read time is exactly what causes streak bugs across
DST changes and users near a UTC day boundary. By storing the local day as
the canonical fact at write time, streak math becomes plain date-interval
arithmetic and never needs to touch timezones again.
"""

import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, field_validator

DB_PATH = os.environ.get("DB_PATH", "habits.db")

app = FastAPI(title="Habit Tracker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                timezone TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id INTEGER NOT NULL REFERENCES habits(id),
                local_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(habit_id, local_date)
            )
        """)


@app.on_event("startup")
def on_startup():
    init_db()


# --------------------------------------------------------------------------
# Password hashing (stdlib PBKDF2 — no external crypto dependency needed)
# --------------------------------------------------------------------------

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return digest.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    digest, _ = hash_password(password, salt)
    return secrets.compare_digest(digest, expected_hash)


# --------------------------------------------------------------------------
# Timezone / local-day logic (the core of this assignment)
# --------------------------------------------------------------------------

def validate_timezone(tz_name: str) -> None:
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise HTTPException(400, f"'{tz_name}' is not a recognized IANA timezone")


def local_today(tz_name: str) -> date:
    """The single source of truth for 'what day is it' for a given user."""
    return datetime.now(ZoneInfo(tz_name)).date()


def compute_streaks(local_dates: list[str], tz_name: str) -> dict:
    """Given a habit's set of local check-in dates, compute current + longest streak."""
    if not local_dates:
        return {"current_streak": 0, "longest_streak": 0}

    days = sorted({date.fromisoformat(d) for d in local_dates})

    # Longest streak: longest run of consecutive calendar days.
    longest = 1
    run = 1
    for i in range(1, len(days)):
        if days[i] == days[i - 1] + timedelta(days=1):
            run += 1
        else:
            run = 1
        longest = max(longest, run)

    # Current streak: anchored on the user's local "today".
    today = local_today(tz_name)
    day_set = set(days)
    if today in day_set:
        anchor = today
    elif today - timedelta(days=1) in day_set:
        # Yesterday was checked in and today isn't over yet — streak alive,
        # just not extended yet today.
        anchor = today - timedelta(days=1)
    else:
        return {"current_streak": 0, "longest_streak": longest}

    current = 0
    cursor = anchor
    while cursor in day_set:
        current += 1
        cursor -= timedelta(days=1)

    return {"current_streak": current, "longest_streak": longest}


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    timezone: str

    @field_validator("password")
    @classmethod
    def password_length(cls, v):
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class HabitCreate(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v):
        if not v.strip():
            raise ValueError("habit name is required")
        return v.strip()


class CheckinCreate(BaseModel):
    local_date: str | None = None  # defaults to "today" in the user's timezone if omitted

    @field_validator("local_date")
    @classmethod
    def valid_date_format(cls, v):
        if v is not None:
            try:
                date.fromisoformat(v)
            except ValueError:
                raise ValueError("local_date must be in YYYY-MM-DD format")
        return v


# --------------------------------------------------------------------------
# Auth dependency
# --------------------------------------------------------------------------

def get_current_user(authorization: str | None = Header(default=None)) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    with get_db() as db:
        session = db.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not session:
            raise HTTPException(401, "invalid or expired session")
        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not user:
            raise HTTPException(401, "user not found")
    return user


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.post("/api/auth/register")
def register(payload: RegisterRequest):
    validate_timezone(payload.timezone)
    password_hash, salt = hash_password(payload.password)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with get_db() as db:
        existing = db.execute("SELECT 1 FROM users WHERE email = ?", (payload.email,)).fetchone()
        if existing:
            raise HTTPException(409, "an account with this email already exists")

        cur = db.execute(
            "INSERT INTO users (email, password_hash, salt, timezone, created_at) VALUES (?, ?, ?, ?, ?)",
            (payload.email, password_hash, salt, payload.timezone, ts),
        )
        user_id = cur.lastrowid
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, ts),
        )

    return {"token": token, "email": payload.email, "timezone": payload.timezone}


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email = ?", (payload.email,)).fetchone()
        if not user or not verify_password(payload.password, user["salt"], user["password_hash"]):
            raise HTTPException(401, "invalid email or password")

        token = secrets.token_urlsafe(32)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user["id"], ts),
        )

    return {"token": token, "email": user["email"], "timezone": user["timezone"]}


# --------------------------------------------------------------------------
# Habit routes
# --------------------------------------------------------------------------

@app.post("/api/habits")
def create_habit(payload: HabitCreate, user=Depends(get_current_user)):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO habits (user_id, name, created_at) VALUES (?, ?, ?)",
            (user["id"], payload.name, ts),
        )
        habit_id = cur.lastrowid
    return {"id": habit_id, "name": payload.name, "created_at": ts}


@app.get("/api/habits")
def list_habits(user=Depends(get_current_user)):
    with get_db() as db:
        habits = db.execute(
            "SELECT * FROM habits WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
        result = []
        for h in habits:
            checkins = db.execute(
                "SELECT local_date FROM checkins WHERE habit_id = ?", (h["id"],)
            ).fetchall()
            local_dates = [c["local_date"] for c in checkins]
            streaks = compute_streaks(local_dates, user["timezone"])
            result.append({
                "id": h["id"],
                "name": h["name"],
                "created_at": h["created_at"],
                "total_checkins": len(local_dates),
                **streaks,
            })
    return result


@app.get("/api/habits/{habit_id}")
def get_habit(habit_id: int, user=Depends(get_current_user)):
    with get_db() as db:
        h = db.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user["id"])
        ).fetchone()
        if not h:
            raise HTTPException(404, "habit not found")
        checkins = db.execute(
            "SELECT local_date FROM checkins WHERE habit_id = ? ORDER BY local_date DESC",
            (habit_id,),
        ).fetchall()
    local_dates = [c["local_date"] for c in checkins]
    streaks = compute_streaks(local_dates, user["timezone"])
    return {
        "id": h["id"],
        "name": h["name"],
        "created_at": h["created_at"],
        "checkins": local_dates,
        **streaks,
    }


@app.post("/api/habits/{habit_id}/checkins")
def create_checkin(habit_id: int, payload: CheckinCreate, user=Depends(get_current_user)):
    with get_db() as db:
        h = db.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user["id"])
        ).fetchone()
        if not h:
            raise HTTPException(404, "habit not found")

        today = local_today(user["timezone"])
        target_date = date.fromisoformat(payload.local_date) if payload.local_date else today

        if target_date > today:
            raise HTTPException(422, f"cannot log a future date (today is {today.isoformat()} in your timezone)")

        existing = db.execute(
            "SELECT 1 FROM checkins WHERE habit_id = ? AND local_date = ?",
            (habit_id, target_date.isoformat()),
        ).fetchone()
        if existing:
            raise HTTPException(409, f"already checked in for {target_date.isoformat()}")

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO checkins (habit_id, local_date, created_at) VALUES (?, ?, ?)",
            (habit_id, target_date.isoformat(), ts),
        )

        checkins = db.execute("SELECT local_date FROM checkins WHERE habit_id = ?", (habit_id,)).fetchall()

    local_dates = [c["local_date"] for c in checkins]
    streaks = compute_streaks(local_dates, user["timezone"])
    return {"local_date": target_date.isoformat(), **streaks}


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_frontend():
    return FileResponse("static/index.html")
