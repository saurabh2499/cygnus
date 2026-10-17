"""Firestore implementation of the db repository interface.

Implements the same functions as ``db_sqlite`` (so the rest of the app is
unchanged) on top of the 5 collections defined in ``firestore_db``:

  requests · restaurants · conversation_summaries · final_summaries · user_confirmations

The old SQLite model carried a few extra fields (vendor features/area/price_tier,
call transcript/rank, result status, ranking). Firestore is schemaless, so those
ride along as additional fields on the canonical documents:
  - restaurants:          + features, area, price_tier, source, source_url,
                            description, search_location  (used by matching/discovery)
  - conversation_summaries: + status, transcript, rank    (the per-vendor call record)
  - final_summaries:      + result_status, ranking         (engine result view)
  - requests:             + attempt_count, max_retries, next_retry_at, last_error

Connection/auth is handled by ``firestore_db`` (service account on Cloud Run,
emulator locally, or an injected mock in tests).
"""

import json
from datetime import datetime, timezone

from . import firestore_db as fs

DEFAULT_VENDOR_LIMIT = 3
DEFAULT_MAX_RETRIES = 3

_CALL_REPLIES = "call_replies"
_USERS = "users"


def _db():
    return fs.client()


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Schema / init (no-op for Firestore; collections are implicit)
# --------------------------------------------------------------------------- #

def init_db() -> None:
    """Firestore needs no schema creation; nothing to do."""
    return None


# --------------------------------------------------------------------------- #
# requests  (requirements + job lifecycle)
# --------------------------------------------------------------------------- #

def register_request(request_id: str, requirements: dict,
                     max_retries: int = DEFAULT_MAX_RETRIES) -> None:
    """Stores a request and enqueues it (status 'pending'), idempotently."""
    doc = _db().collection(fs.REQUESTS).document(request_id)
    if doc.get().exists:
        return
    fs.create_request(request_id, requirements, max_retries=max_retries)


def get_requirements(request_id: str) -> dict | None:
    r = fs.get_request(request_id)
    return r.get("requirements_payload") if r else None


def sync_jobs_from_requests(max_retries: int = DEFAULT_MAX_RETRIES) -> int:
    """No-op: in Firestore a request IS its own job (register_request creates it)."""
    return 0


def claim_due_jobs(limit: int) -> list[dict]:
    """Atomically claims due requests: 'pending', or 'retry_scheduled' past its
    next_retry_at. Marks them 'in_progress' and bumps attempt_count."""
    now = _now()
    candidates = []
    col = _db().collection(fs.REQUESTS)
    for status in ("pending", "retry_scheduled"):
        for snap in col.where("request_status", "==", status).stream():
            d = snap.to_dict()
            if status == "retry_scheduled":
                nxt = d.get("next_retry_at")
                if nxt is not None and _as_dt(nxt) > now:
                    continue
            candidates.append(d)
    candidates.sort(key=lambda d: _as_dt(d.get("created_on")) or now)
    claimed = []
    for d in candidates[:max(0, limit)]:
        rid = d["id"]
        attempt = (d.get("attempt_count") or 0) + 1
        fs.update_request(rid, request_status="in_progress", attempt_count=attempt,
                          stage="picked")
        d = fs.get_request(rid)
        # Normalize to the same keys the cron's _run_job expects (SQLite jobs schema):
        d["request_id"] = d.get("id")
        d["status"] = d.get("request_status")
        claimed.append(d)
    return claimed


def set_request_stage(request_id: str, stage: str) -> None:
    """Sets a short progress stage (queued/picked/running/calling/completed)."""
    fs.update_request(request_id, stage=stage)


def complete_job(request_id: str) -> None:
    fs.update_request(request_id, request_status="completed", stage="completed",
                      last_error=None, next_retry_at=None)


def fail_or_retry_job(request_id: str, error: str, backoff_seconds: int = 60) -> str:
    r = fs.get_request(request_id)
    if r is None:
        return "unknown"
    if (r.get("attempt_count") or 0) >= (r.get("max_retries") or DEFAULT_MAX_RETRIES):
        fs.update_request(request_id, request_status="failed", last_error=error,
                          next_retry_at=None)
        return "failed"
    from datetime import timedelta
    fs.update_request(request_id, request_status="retry_scheduled", last_error=error,
                      next_retry_at=_now() + timedelta(seconds=backoff_seconds))
    return "retry_scheduled"


# --------------------------------------------------------------------------- #
# restaurants  (the vendor catalog)
# --------------------------------------------------------------------------- #

def _restaurant_to_vendor(d: dict) -> dict:
    """Maps a restaurant doc to the vendor dict shape the tools expect."""
    loc = d.get("location")
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "phone": d.get("contact"),
        "area": d.get("area"),
        "capacity": d.get("max_seating_capacity"),
        "price_tier": d.get("price_tier"),
        "rating": d.get("rating"),
        "cuisine_types": d.get("cuisine_types") or [],
        "features": d.get("features") or [],
        "source": d.get("source", "seed"),
        "source_url": d.get("source_url"),
        "description": d.get("description") or d.get("comments_from_restaurant"),
        "search_location": d.get("search_location"),
        "lat": getattr(loc, "latitude", None),
        "lon": getattr(loc, "longitude", None),
    }


def list_vendors() -> list[dict]:
    return [_restaurant_to_vendor(d) for d in fs.list_restaurants()]


def upsert_vendor(vendor: dict) -> str:
    """Upserts a restaurant, preserving both canonical and operational fields."""
    rid = vendor["id"]
    doc = _db().collection(fs.RESTAURANTS).document(rid)
    data = {
        "id": rid,
        "name": vendor.get("name"),
        "contact": vendor.get("phone"),
        "max_seating_capacity": vendor.get("capacity"),
        "rating": vendor.get("rating"),
        "cuisine_types": vendor.get("cuisine_types") or [],
        "comments_from_restaurant": vendor.get("comments_from_restaurant"),
        # operational / discovery fields (schemaless extras)
        "area": vendor.get("area"),
        "price_tier": vendor.get("price_tier"),
        "features": vendor.get("features") or [],
        "source": vendor.get("source", "web"),
        "source_url": vendor.get("source_url"),
        "description": vendor.get("description"),
        "search_location": vendor.get("search_location"),
        "modified_on": fs._server_ts(),
    }
    lat, lon = vendor.get("lat"), vendor.get("lon")
    if lat is not None and lon is not None:
        data["location"] = fs._geopoint(lat, lon)
    if not doc.get().exists:
        data["created_on"] = fs._server_ts()
    data = {k: v for k, v in data.items() if v is not None}
    doc.set(data, merge=True)
    return rid


def get_web_vendors_for_location(location: str) -> list[dict]:
    norm = (location or "").strip().lower()
    if not norm:
        return []
    col = _db().collection(fs.RESTAURANTS)
    out = []
    for snap in col.where("source", "==", "web").stream():
        d = snap.to_dict()
        if (d.get("search_location") or "") == norm:
            out.append(_restaurant_to_vendor(d))
    return out


# --------------------------------------------------------------------------- #
# conversation_summaries  (the per-vendor call record)
# --------------------------------------------------------------------------- #

def get_calls(request_id: str) -> list[dict]:
    out = []
    for d in fs.get_conversation_summaries(request_id):
        out.append({
            "request_id": request_id,
            "vendor_id": d.get("restaurant_id"),
            "status": d.get("status", "called"),
            "transcript": d.get("transcript"),
            "summary": d.get("summary"),
            "rank": d.get("rank"),
            "approved": _is_approved(request_id, d.get("restaurant_id")),
        })
    return out


def record_call(request_id: str, vendor_id: str, status: str,
                transcript: str | None = None) -> None:
    doc_id = fs._pair_id(request_id, vendor_id)
    doc = _db().collection(fs.CONVERSATION_SUMMARIES).document(doc_id)
    data = {"request_id": request_id, "restaurant_id": vendor_id,
            "status": status, "transcript": transcript}
    if not doc.get().exists:
        data["created_on"] = fs._server_ts()
    doc.set(data, merge=True)


def set_vendor_summary(request_id: str, vendor_id: str, summary: str,
                       rank: int | None = None) -> bool:
    doc_id = fs._pair_id(request_id, vendor_id)
    doc = _db().collection(fs.CONVERSATION_SUMMARIES).document(doc_id)
    if not doc.get().exists:
        return False
    doc.set({"summary": summary, "rank": rank, "status": "summarized"}, merge=True)
    return True


# --------------------------------------------------------------------------- #
# final_summaries + requests result view
# --------------------------------------------------------------------------- #

def set_final_summary(request_id: str, status: str,
                      final_summary: str | None = None,
                      ranking: list | None = None) -> None:
    doc = _db().collection(fs.FINAL_SUMMARIES).document(request_id)
    data = {"request_id": request_id, "result_status": status,
            "modified_on": fs._server_ts()}
    if final_summary is not None:
        data["summary"] = final_summary
    if ranking is not None:
        data["ranking"] = ranking
    if not doc.get().exists:
        data["created_on"] = fs._server_ts()
    doc.set(data, merge=True)


def get_request_result(request_id: str) -> dict | None:
    req = fs.get_request(request_id)
    final = fs.get_final_summary(request_id)
    calls = get_calls(request_id)
    if req is None and final is None and not calls:
        return None
    requirements = (req or {}).get("requirements_payload") or {}
    calls_sorted = sorted(calls, key=lambda c: (c["rank"] is None, c["rank"] or 0))
    return {
        "request_id": request_id,
        "requirements": requirements,
        "stage": (req or {}).get("stage"),
        "job_status": (req or {}).get("request_status", "not_queued"),
        "result_status": (final or {}).get("result_status", "pending"),
        "final_summary": (final or {}).get("summary"),
        "ranking": (final or {}).get("ranking") or [],
        "vendors": [
            {"vendor_id": c["vendor_id"], "status": c["status"], "rank": c["rank"],
             "summary": c["summary"], "approved": c["approved"]}
            for c in calls_sorted
        ],
    }


# --------------------------------------------------------------------------- #
# user_confirmations  (approval)
# --------------------------------------------------------------------------- #

def _is_approved(request_id: str, vendor_id: str) -> bool:
    doc_id = fs._pair_id(request_id, vendor_id)
    snap = _db().collection(fs.USER_CONFIRMATIONS).document(doc_id).get()
    return bool(snap.exists and snap.to_dict().get("status") == fs.STATUS_COMPLETED)


def set_vendor_approval(request_id: str, vendor_id: str, approved: bool) -> bool:
    doc_id = fs._pair_id(request_id, vendor_id)
    if not _db().collection(fs.CONVERSATION_SUMMARIES).document(doc_id).get().exists:
        return False
    fs.set_user_confirmation(request_id, vendor_id,
                             fs.STATUS_COMPLETED if approved else fs.STATUS_NOT_COMPLETED)
    return True


# --------------------------------------------------------------------------- #
# list_requests  (dashboard)
# --------------------------------------------------------------------------- #

def list_requests() -> list[dict]:
    rows = []
    for req in fs.list_requests():
        reqs = req.get("requirements_payload") or {}
        final = fs.get_final_summary(req["id"])
        rows.append({
            "request_id": req["id"],
            "event_type": reqs.get("event_type"),
            "guest_count": reqs.get("guest_count"),
            "location": reqs.get("location"),
            "stage": req.get("stage"),
            "job_status": req.get("request_status", "not_queued"),
            "result_status": (final or {}).get("result_status", "pending"),
            "attempt_count": req.get("attempt_count", 0),
            "max_retries": req.get("max_retries"),
            "updated_on": req.get("modified_on"),
        })
    rows.sort(key=lambda r: str(r.get("updated_on") or ""), reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# call_replies  (live-call speech capture; operational collection)
# --------------------------------------------------------------------------- #

def create_user(email: str, password_hash: str, account_type: str,
                name: str | None = None, company: str | None = None,
                team_budget: float | None = None, budget_currency: str | None = None,
                budget_expiry: str | None = None) -> dict | None:
    """Creates a user doc (id = email). Returns None if it already exists."""
    doc = _db().collection(_USERS).document(email)
    if doc.get().exists:
        return None
    doc.set({
        "email": email, "password_hash": password_hash, "account_type": account_type,
        "name": name, "company": company, "team_budget": team_budget,
        "budget_currency": budget_currency, "budget_expiry": budget_expiry,
        "created_on": fs._server_ts(),
    })
    return get_user(email)


def get_user(email: str) -> dict | None:
    snap = _db().collection(_USERS).document(email).get()
    return snap.to_dict() if snap.exists else None


def set_call_reply(token: str, text: str) -> None:
    _db().collection(_CALL_REPLIES).document(token).set(
        {"token": token, "text": text, "created_on": fs._server_ts()})


def get_call_reply(token: str) -> str | None:
    snap = _db().collection(_CALL_REPLIES).document(token).get()
    return snap.to_dict().get("text") if snap.exists else None


_CALL_STATE = "call_state"


def set_call_state(token: str, data: dict) -> None:
    _db().collection(_CALL_STATE).document(token).set(
        {**data, "token": token, "updated_on": fs._server_ts()})


def get_call_state(token: str) -> dict | None:
    snap = _db().collection(_CALL_STATE).document(token).get()
    return snap.to_dict() if snap.exists else None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _as_dt(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None
