"""Database facade — selects the storage backend at import time.

Set DB_BACKEND to choose where data lives (default: sqlite for local dev):
  DB_BACKEND=sqlite     -> engine_agent/db_sqlite.py  (local file, no cloud)
  DB_BACKEND=firestore  -> engine_agent/db_firestore.py (Firestore on GCP/Cloud Run)

Both backends expose the same function interface, so the rest of the app imports
`from engine_agent import db` and calls `db.<fn>()` without caring which backend
is active. The Firestore backend maps onto the 5 collections in `firestore_db`.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load the project-root .env once, for every component, regardless of backend.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_BACKEND = os.environ.get("DB_BACKEND", "sqlite").strip().lower()

if _BACKEND == "firestore":
    from .db_firestore import (  # noqa: F401
        DEFAULT_MAX_RETRIES,
        DEFAULT_VENDOR_LIMIT,
        claim_due_jobs,
        complete_job,
        create_user,
        fail_or_retry_job,
        get_call_reply,
        get_user,
        get_calls,
        get_request_result,
        get_requirements,
        get_web_vendors_for_location,
        init_db,
        list_requests,
        list_vendors,
        record_call,
        register_request,
        set_call_reply,
        set_final_summary,
        set_vendor_approval,
        set_vendor_summary,
        sync_jobs_from_requests,
        upsert_vendor,
    )
else:
    from .db_sqlite import (  # noqa: F401
        DEFAULT_MAX_RETRIES,
        DEFAULT_VENDOR_LIMIT,
        claim_due_jobs,
        complete_job,
        connect,
        create_user,
        fail_or_retry_job,
        get_call_reply,
        get_calls,
        get_request_result,
        get_requirements,
        get_user,
        get_web_vendors_for_location,
        init_db,
        list_requests,
        list_vendors,
        record_call,
        register_request,
        set_call_reply,
        set_final_summary,
        set_vendor_approval,
        set_vendor_summary,
        sync_jobs_from_requests,
        upsert_vendor,
    )


def backend() -> str:
    """Returns the active backend name ('sqlite' or 'firestore')."""
    return _BACKEND
