"""Shared SQLite store for the vendor-management system.

The engine agent reads the approved requirements written by the chatbot agent
(``cygnus``) and writes its own results (per-vendor call records, per-vendor
summaries, and the final ranked summary) into the same database file so the
whole system shares one store.

The DB path can be overridden with the ``VENDOR_DB_PATH`` env var; by default it
points at the chatbot agent's ``requirements.db`` so the data lines up out of
the box.
"""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

# Load the project-root .env so the cron job, backend, and agents all pick up
# ANTHROPIC_API_KEY (and VENDOR_DB_PATH) from one place. This module is imported
# by every component, so loading here covers them all. Existing environment
# variables win over the file.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "cygnus" / "requirements.db"
DB_PATH = Path(os.environ.get("VENDOR_DB_PATH", _DEFAULT_DB))

# How many best-fit vendors the engine contacts per request, by default.
DEFAULT_VENDOR_LIMIT = 3

# Default max processing attempts per job before it is marked failed (cron).
DEFAULT_MAX_RETRIES = 3


def connect() -> sqlite3.Connection:
    """Opens a connection with row access by column name and FK enforcement."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Creates the engine's tables if they don't exist and seeds sample vendors.

    The chatbot agent owns ``event_requirements``; we only read from it, so we
    don't (re)create it here.
    """
    with closing(connect()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vendors (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                phone         TEXT,
                area          TEXT,
                capacity      INTEGER,
                price_tier    INTEGER,          -- 1 (budget) .. 4 (premium)
                features      TEXT NOT NULL      -- JSON list of feature tags
            );

            CREATE TABLE IF NOT EXISTS vendor_calls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id  TEXT NOT NULL,
                vendor_id   TEXT NOT NULL,
                status      TEXT NOT NULL,        -- called | summarized | failed
                transcript  TEXT,
                summary     TEXT,
                rank        INTEGER,
                approved    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (request_id, vendor_id)
            );

            CREATE TABLE IF NOT EXISTS request_summaries (
                request_id    TEXT PRIMARY KEY,
                status        TEXT NOT NULL,      -- processing | completed | failed
                final_summary TEXT,
                ranking       TEXT,               -- JSON list of ranked vendors
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- User accounts (lightweight auth): individual or corporate.
            CREATE TABLE IF NOT EXISTS users (
                email          TEXT PRIMARY KEY,
                password_hash  TEXT NOT NULL,
                account_type   TEXT NOT NULL,      -- individual | corporate
                name           TEXT,
                company        TEXT,
                team_budget    REAL,               -- corporate only
                budget_currency TEXT,              -- corporate only
                budget_expiry  TEXT,               -- corporate only (ISO date)
                created_on     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Spoken replies captured from live Twilio calls, keyed by a token
            -- passed in the TwiML webhook.
            CREATE TABLE IF NOT EXISTS call_replies (
                token      TEXT PRIMARY KEY,
                text       TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Work queue the cron job polls: one job per request.
            CREATE TABLE IF NOT EXISTS jobs (
                request_id    TEXT PRIMARY KEY,
                status        TEXT NOT NULL,      -- pending | in_progress | retry_scheduled | completed | failed
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_retries   INTEGER NOT NULL,
                next_retry_at TEXT,               -- ISO timestamp; job eligible when now >= this
                last_error    TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        _migrate(conn)
        _seed_vendors(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Adds columns introduced after a DB was first created (best-effort)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(vendor_calls)")}
    if "approved" not in cols:
        conn.execute(
            "ALTER TABLE vendor_calls ADD COLUMN approved INTEGER NOT NULL DEFAULT 0"
        )
    # Web-discovery columns on vendors.
    vcols = {r["name"] for r in conn.execute("PRAGMA table_info(vendors)")}
    if "source" not in vcols:
        conn.execute("ALTER TABLE vendors ADD COLUMN source TEXT NOT NULL DEFAULT 'seed'")
    if "source_url" not in vcols:
        conn.execute("ALTER TABLE vendors ADD COLUMN source_url TEXT")
    if "description" not in vcols:
        conn.execute("ALTER TABLE vendors ADD COLUMN description TEXT")
    if "search_location" not in vcols:
        # The normalized location this venue was discovered for (cache key).
        conn.execute("ALTER TABLE vendors ADD COLUMN search_location TEXT")


def _seed_vendors(conn: sqlite3.Connection) -> None:
    """Inserts a small sample vendor catalog the first time the DB is created."""
    existing = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    if existing:
        return
    sample = [
        ("v_olive", "The Olive Table", "+1-555-0101", "downtown", 120, 3,
         ["catering", "private_room", "vegan", "av", "italian"]),
        ("v_garden", "Garden Courtyard", "+1-555-0102", "downtown", 200, 2,
         ["catering", "outdoor", "parking", "live_music", "decor"]),
        ("v_sakura", "Sakura Hall", "+1-555-0103", "midtown", 80, 4,
         ["catering", "private_room", "av", "japanese", "vegan"]),
        ("v_harbor", "Harbor Lights", "+1-555-0104", "waterfront", 300, 4,
         ["catering", "outdoor", "live_music", "parking", "av", "decor"]),
        ("v_corner", "Corner Bistro", "+1-555-0105", "suburbs", 50, 1,
         ["catering", "parking", "kids_friendly"]),
    ]
    conn.executemany(
        "INSERT INTO vendors (id, name, phone, area, capacity, price_tier, features)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(vid, name, phone, area, cap, tier, json.dumps(feats))
         for vid, name, phone, area, cap, tier, feats in sample],
    )


def get_requirements(request_id: str) -> dict | None:
    """Returns the approved requirements dict for a request, or None if absent."""
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT requirements FROM event_requirements WHERE requestid = ?",
            (request_id,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["requirements"])


def list_vendors() -> list[dict]:
    """Returns the full vendor catalog with features decoded to lists."""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM vendors").fetchall()
    vendors = []
    for r in rows:
        v = dict(r)
        v["features"] = json.loads(v["features"])
        vendors.append(v)
    return vendors


def upsert_vendor(vendor: dict) -> str:
    """Inserts or updates a vendor (used to cache web-discovered venues).

    Expects keys: id, name, and optionally phone, area, capacity, price_tier,
    features (list), source, source_url, description. Returns the vendor id.
    """
    v = {
        "id": vendor["id"], "name": vendor["name"], "phone": vendor.get("phone"),
        "area": vendor.get("area"), "capacity": vendor.get("capacity"),
        "price_tier": vendor.get("price_tier"),
        "features": json.dumps(vendor.get("features", [])),
        "source": vendor.get("source", "web"),
        "source_url": vendor.get("source_url"),
        "description": vendor.get("description"),
        "search_location": vendor.get("search_location"),
    }
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO vendors (id, name, phone, area, capacity, price_tier,"
            "   features, source, source_url, description, search_location)"
            " VALUES (:id, :name, :phone, :area, :capacity, :price_tier,"
            "   :features, :source, :source_url, :description, :search_location)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   name=excluded.name, phone=COALESCE(excluded.phone, vendors.phone),"
            "   area=COALESCE(excluded.area, vendors.area),"
            "   capacity=COALESCE(excluded.capacity, vendors.capacity),"
            "   price_tier=COALESCE(excluded.price_tier, vendors.price_tier),"
            "   features=excluded.features, source=excluded.source,"
            "   source_url=COALESCE(excluded.source_url, vendors.source_url),"
            "   description=COALESCE(excluded.description, vendors.description),"
            "   search_location=COALESCE(excluded.search_location, vendors.search_location)",
            v,
        )
    return v["id"]


def create_user(email: str, password_hash: str, account_type: str,
                name: str | None = None, company: str | None = None,
                team_budget: float | None = None, budget_currency: str | None = None,
                budget_expiry: str | None = None) -> dict | None:
    """Creates a user. Returns the user dict, or None if the email already exists."""
    init_db()
    try:
        with closing(connect()) as conn, conn:
            conn.execute(
                "INSERT INTO users (email, password_hash, account_type, name, company,"
                " team_budget, budget_currency, budget_expiry) VALUES (?,?,?,?,?,?,?,?)",
                (email, password_hash, account_type, name, company,
                 team_budget, budget_currency, budget_expiry),
            )
    except sqlite3.IntegrityError:
        return None
    return get_user(email)


def get_user(email: str) -> dict | None:
    """Returns the full user record (including password_hash) or None."""
    init_db()
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def set_call_reply(token: str, text: str) -> None:
    """Stores a spoken reply captured from a live call (Twilio webhook)."""
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO call_replies (token, text) VALUES (?, ?)"
            " ON CONFLICT(token) DO UPDATE SET text = excluded.text",
            (token, text),
        )


def get_call_reply(token: str) -> str | None:
    """Returns the stored spoken reply for a call token, if captured yet."""
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT text FROM call_replies WHERE token = ?", (token,)
        ).fetchone()
    return row["text"] if row else None


def get_web_vendors_for_location(location: str) -> list[dict]:
    """Returns previously web-discovered venues cached for this location.

    The location is matched case-insensitively against the stored search
    location, so a repeat request for the same place reuses the cache instead of
    searching again.
    """
    norm = (location or "").strip().lower()
    if not norm:
        return []
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM vendors WHERE source = 'web' AND search_location = ?",
            (norm,),
        ).fetchall()
    out = []
    for r in rows:
        v = dict(r)
        v["features"] = json.loads(v["features"])
        out.append(v)
    return out


def get_calls(request_id: str) -> list[dict]:
    """Returns all call records recorded so far for a request."""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM vendor_calls WHERE request_id = ? ORDER BY id",
            (request_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_call(request_id: str, vendor_id: str, status: str,
                transcript: str | None = None) -> None:
    """Upserts a call record for (request_id, vendor_id)."""
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO vendor_calls (request_id, vendor_id, status, transcript)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(request_id, vendor_id) DO UPDATE SET"
            "   status = excluded.status, transcript = excluded.transcript",
            (request_id, vendor_id, status, transcript),
        )


def set_vendor_summary(request_id: str, vendor_id: str, summary: str,
                       rank: int | None = None) -> bool:
    """Stores the per-vendor summary (and optional rank). False if no such call."""
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE vendor_calls SET summary = ?, rank = ?, status = 'summarized'"
            " WHERE request_id = ? AND vendor_id = ?",
            (summary, rank, request_id, vendor_id),
        )
    return cur.rowcount > 0


def set_final_summary(request_id: str, status: str,
                      final_summary: str | None = None,
                      ranking: list | None = None) -> None:
    """Upserts the request-level status, final summary, and ranking."""
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO request_summaries (request_id, status, final_summary, ranking, updated_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(request_id) DO UPDATE SET"
            "   status = excluded.status,"
            "   final_summary = COALESCE(excluded.final_summary, request_summaries.final_summary),"
            "   ranking = COALESCE(excluded.ranking, request_summaries.ranking),"
            "   updated_at = excluded.updated_at",
            (request_id, status, final_summary,
             json.dumps(ranking) if ranking is not None else None),
        )


# --------------------------------------------------------------------------- #
# Job queue (used by the cron job)
# --------------------------------------------------------------------------- #

def register_request(request_id: str, requirements: dict,
                     max_retries: int = DEFAULT_MAX_RETRIES) -> None:
    """Stores a request's requirements and enqueues a job for it (idempotent).

    This is what the backend's "register request" endpoint calls. If the request
    already exists it is left as-is rather than duplicated.
    """
    init_db()
    with closing(connect()) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS event_requirements ("
            "requestid TEXT PRIMARY KEY, requirements TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_requirements (requestid, requirements)"
            " VALUES (?, ?)",
            (request_id, json.dumps(requirements)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO jobs (request_id, status, max_retries)"
            " VALUES (?, 'pending', ?)",
            (request_id, max_retries),
        )


def sync_jobs_from_requests(max_retries: int = DEFAULT_MAX_RETRIES) -> int:
    """Enqueues jobs for approved requests that don't have one yet.

    Bridges requests the chatbot wrote straight to ``event_requirements`` (not via
    the register endpoint) into the job queue. Returns how many jobs were created.
    """
    with closing(connect()) as conn, conn:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='event_requirements'"
        ).fetchone()
        if not has_table:
            return 0
        cur = conn.execute(
            "INSERT INTO jobs (request_id, status, max_retries)"
            " SELECT r.requestid, 'pending', ?"
            " FROM event_requirements r"
            " LEFT JOIN jobs j ON j.request_id = r.requestid"
            " WHERE j.request_id IS NULL",
            (max_retries,),
        )
    return cur.rowcount


def claim_due_jobs(limit: int) -> list[dict]:
    """Atomically claims up to ``limit`` jobs that are due, marking them in_progress.

    Due means status 'pending', or 'retry_scheduled' whose ``next_retry_at`` has
    passed. Claiming in a single UPDATE makes it safe against concurrent cron runs:
    a job claimed by one run won't be returned to another.
    """
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT request_id FROM jobs"
            " WHERE status = 'pending'"
            "    OR (status = 'retry_scheduled' AND next_retry_at <= datetime('now'))"
            " ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        claimed = [r["request_id"] for r in rows]
        for rid in claimed:
            conn.execute(
                "UPDATE jobs SET status = 'in_progress',"
                " attempt_count = attempt_count + 1, updated_at = datetime('now')"
                " WHERE request_id = ?",
                (rid,),
            )
        jobs = [
            dict(conn.execute("SELECT * FROM jobs WHERE request_id = ?", (rid,)).fetchone())
            for rid in claimed
        ]
    return jobs


def complete_job(request_id: str) -> None:
    """Marks a job completed after a successful engine run."""
    with closing(connect()) as conn, conn:
        conn.execute(
            "UPDATE jobs SET status = 'completed', last_error = NULL,"
            " next_retry_at = NULL, updated_at = datetime('now')"
            " WHERE request_id = ?",
            (request_id,),
        )


def fail_or_retry_job(request_id: str, error: str, backoff_seconds: int = 60) -> str:
    """Records a failed attempt: reschedules for retry, or fails if cap reached.

    Returns the new job status ('retry_scheduled' or 'failed').
    """
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT attempt_count, max_retries FROM jobs WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return "unknown"
        if row["attempt_count"] >= row["max_retries"]:
            new_status, next_retry = "failed", None
        else:
            new_status = "retry_scheduled"
            next_retry = f"+{backoff_seconds} seconds"
        conn.execute(
            "UPDATE jobs SET status = ?, last_error = ?,"
            " next_retry_at = CASE WHEN ? IS NULL THEN NULL"
            "   ELSE datetime('now', ?) END,"
            " updated_at = datetime('now')"
            " WHERE request_id = ?",
            (new_status, error, next_retry, next_retry, request_id),
        )
    return new_status


# --------------------------------------------------------------------------- #
# Read model for the backend API
# --------------------------------------------------------------------------- #

def get_request_result(request_id: str) -> dict | None:
    """Assembles the full result view for a request, or None if unknown.

    Returns job status, the final summary and ranking, and the per-vendor
    summaries — everything the "get summary" endpoint needs to return.
    """
    init_db()
    requirements = get_requirements(request_id)
    with closing(connect()) as conn:
        summary = conn.execute(
            "SELECT * FROM request_summaries WHERE request_id = ?", (request_id,)
        ).fetchone()
        job = conn.execute(
            "SELECT * FROM jobs WHERE request_id = ?", (request_id,)
        ).fetchone()
        calls = conn.execute(
            "SELECT * FROM vendor_calls WHERE request_id = ? ORDER BY rank IS NULL, rank",
            (request_id,),
        ).fetchall()

    if requirements is None and summary is None and job is None:
        return None

    return {
        "request_id": request_id,
        "requirements": requirements or {},
        "job_status": job["status"] if job else "not_queued",
        "result_status": summary["status"] if summary else "pending",
        "final_summary": summary["final_summary"] if summary else None,
        "ranking": json.loads(summary["ranking"]) if summary and summary["ranking"] else [],
        "vendors": [
            {
                "vendor_id": c["vendor_id"],
                "status": c["status"],
                "rank": c["rank"],
                "summary": c["summary"],
                "approved": bool(c["approved"]),
            }
            for c in calls
        ],
    }


def list_requests() -> list[dict]:
    """Returns a summary row for every request, newest activity first.

    Used by the dashboard's request list. Joins the requirements, the job state,
    and the result status for each request.
    """
    init_db()
    with closing(connect()) as conn:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='event_requirements'"
        ).fetchone()
        if not has_table:
            return []
        rows = conn.execute(
            "SELECT r.requestid AS request_id, r.requirements AS requirements,"
            "       j.status AS job_status, j.attempt_count, j.max_retries,"
            "       j.created_at AS job_created, s.status AS result_status,"
            "       s.updated_at AS result_updated"
            "  FROM event_requirements r"
            "  LEFT JOIN jobs j ON j.request_id = r.requestid"
            "  LEFT JOIN request_summaries s ON s.request_id = r.requestid"
            " ORDER BY COALESCE(s.updated_at, j.created_at) DESC"
        ).fetchall()
    out = []
    for row in rows:
        reqs = json.loads(row["requirements"]) if row["requirements"] else {}
        out.append({
            "request_id": row["request_id"],
            "event_type": reqs.get("event_type"),
            "guest_count": reqs.get("guest_count"),
            "location": reqs.get("location"),
            "job_status": row["job_status"] or "not_queued",
            "result_status": row["result_status"] or "pending",
            "attempt_count": row["attempt_count"] or 0,
            "max_retries": row["max_retries"],
            "updated_at": row["result_updated"] or row["job_created"],
        })
    return out


def set_vendor_approval(request_id: str, vendor_id: str, approved: bool) -> bool:
    """Marks a vendor approved/unapproved for a request. False if no such call."""
    init_db()
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE vendor_calls SET approved = ? WHERE request_id = ? AND vendor_id = ?",
            (1 if approved else 0, request_id, vendor_id),
        )
    return cur.rowcount > 0
