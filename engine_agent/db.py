"""Database facade — selects the storage backend at import time.

Set DB_BACKEND to choose where data lives (default: sqlite for local dev):
  DB_BACKEND=sqlite     -> engine_agent/db_sqlite.py  (local file, no cloud)
  DB_BACKEND=firestore  -> engine_agent/db_firestore.py (Firestore on GCP/Cloud Run)

Both backends expose the same function interface, so the rest of the app imports
`from engine_agent import db` and calls `db.<fn>()` without caring which backend
is active. The Firestore backend maps onto the 5 collections in `firestore_db`.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load the project-root .env once, for every component, regardless of backend.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Silence google-genai's noisy "Direct use of AFC..." WARNING (we don't use AFC
# in our direct generate_content calls). Keeps ERROR-level logs intact.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)
logging.getLogger("google_genai.types").setLevel(logging.ERROR)

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
        get_call_state,
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
        set_call_state,
        set_final_summary,
        set_request_stage,
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
        get_call_state,
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
        set_call_state,
        set_final_summary,
        set_request_stage,
        set_vendor_approval,
        set_vendor_summary,
        sync_jobs_from_requests,
        upsert_vendor,
    )


def backend() -> str:
    """Returns the active backend name ('sqlite' or 'firestore')."""
    return _BACKEND
