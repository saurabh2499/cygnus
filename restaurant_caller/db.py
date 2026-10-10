"""SQLite access shared by the chat agent and the call server.

Both processes read and write the same database (the one the event
requirements agent writes approved requirements to), so call progress made
by the call server is visible to the chat agent.
"""

import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Optional

DB_PATH = Path(
    os.environ.get(
        "EVENT_DB_PATH",
        Path(__file__).resolve().parent.parent / "cygnus" / "requirements.db",
    )
)

CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurant_calls (
    call_id            TEXT PRIMARY KEY,
    request_id         TEXT NOT NULL,
    restaurant_id      TEXT NOT NULL,
    provider_call_id   TEXT,
    status             TEXT NOT NULL,
    verdict            TEXT,
    summary            TEXT,
    unmet_requirements TEXT,
    requirement_results TEXT,
    transcript         TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_calls_table(conn: sqlite3.Connection) -> None:
    conn.execute(CALLS_SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(restaurant_calls)")}
    # Tables created while the agent used Twilio have a Twilio-specific column name.
    if "twilio_call_sid" in columns:
        conn.execute(
            "ALTER TABLE restaurant_calls RENAME COLUMN twilio_call_sid TO provider_call_id"
        )
    if "requirement_results" not in columns:
        conn.execute("ALTER TABLE restaurant_calls ADD COLUMN requirement_results TEXT")


def get_requirements(request_id: str) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT requirements FROM event_requirements WHERE requestid = ?",
            (request_id,),
        ).fetchone()
    return json.loads(row["requirements"]) if row else None


def get_restaurant(restaurant_id: str) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM restaurants WHERE restaurant_id = ?", (restaurant_id,)
        ).fetchone()
    return dict(row) if row else None


def create_call(request_id: str, restaurant_id: str) -> str:
    call_id = str(uuid.uuid4())
    with closing(_connect()) as conn, conn:
        _ensure_calls_table(conn)
        conn.execute(
            "INSERT INTO restaurant_calls (call_id, request_id, restaurant_id, status)"
            " VALUES (?, ?, ?, 'initiating')",
            (call_id, request_id, restaurant_id),
        )
    return call_id


def get_call(call_id: str) -> Optional[dict]:
    with closing(_connect()) as conn, conn:
        _ensure_calls_table(conn)
        row = conn.execute(
            "SELECT * FROM restaurant_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
    if not row:
        return None
    call = dict(row)
    call["transcript"] = json.loads(call["transcript"])
    for field in ("unmet_requirements", "requirement_results"):
        if call[field]:
            call[field] = json.loads(call[field])
    return call


def update_call(call_id: str, **fields) -> None:
    for field in ("unmet_requirements", "requirement_results"):
        if fields.get(field) is not None:
            fields[field] = json.dumps(fields[field])
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with closing(_connect()) as conn, conn:
        conn.execute(
            f"UPDATE restaurant_calls SET {assignments}, updated_at = CURRENT_TIMESTAMP"
            " WHERE call_id = :call_id",
            {**fields, "call_id": call_id},
        )


def append_transcript(call_id: str, speaker: str, text: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE restaurant_calls SET transcript = json_insert(transcript, '$[#]',"
            " json_object('speaker', ?, 'text', ?)), updated_at = CURRENT_TIMESTAMP"
            " WHERE call_id = ?",
            (speaker, text, call_id),
        )
