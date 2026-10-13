"""Firestore data layer — the 5 collections for the vendor-management system.

Firestore has no DDL; a collection exists once its first document is written.
This module defines the collections, their document shape, and the CRUD helpers
the app uses.

Collections
-----------
1. requests
     id (doc id), requirements_payload (map), request_status (str),
     created_on (ts), modified_on (ts)
     [+ operational: attempt_count, max_retries, next_retry_at — for the cron]
2. restaurants
     id (doc id), name, location (GeoPoint: LAT/LON), contact,
     max_seating_capacity (int), rating (float), cuisine_types (list[str]),
     comments_from_restaurant (str), created_on (ts), modified_on (ts)
3. conversation_summaries   (one per request+restaurant)
     request_id, restaurant_id, summary, created_on
4. final_summaries          (one per request; doc id = request_id)
     request_id, summary, created_on, modified_on
5. user_confirmations       (one per request+restaurant)
     request_id, restaurant_id, status in {Completed, In-Queued, Not Completed},
     created_on, modified_on

Connection
----------
- On Cloud Run / GCP: firestore.Client() authenticates via the service account
  (set GOOGLE_CLOUD_PROJECT). No key needed.
- Local with the emulator: set FIRESTORE_EMULATOR_HOST=localhost:8080.
- Tests: call set_client(mockfirestore.MockFirestore()) to inject a fake client.
"""

import os

REQUESTS = "requests"
RESTAURANTS = "restaurants"
CONVERSATION_SUMMARIES = "conversation_summaries"
FINAL_SUMMARIES = "final_summaries"
USER_CONFIRMATIONS = "user_confirmations"

# user_confirmation.status values
STATUS_COMPLETED = "Completed"
STATUS_IN_QUEUED = "In-Queued"
STATUS_NOT_COMPLETED = "Not Completed"

_client = None


def set_client(client) -> None:
    """Injects a Firestore client (real or mock). Call once at startup/tests."""
    global _client
    _client = client


def client():
    """Returns the Firestore client, creating a real one lazily if unset."""
    global _client
    if _client is None:
        from google.cloud import firestore
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        _client = firestore.Client(project=project) if project else firestore.Client()
    return _client


def _server_ts():
    """SERVER_TIMESTAMP sentinel (mock-firestore also understands it)."""
    from google.cloud import firestore
    return firestore.SERVER_TIMESTAMP


def _geopoint(lat, lon):
    from google.cloud.firestore import GeoPoint
    return GeoPoint(float(lat), float(lon))


# --------------------------------------------------------------------------- #
# 1. requests
# --------------------------------------------------------------------------- #

def create_request(request_id: str, requirements_payload: dict,
                   max_retries: int = 3) -> str:
    """Creates a request doc (status 'pending') and returns its id."""
    client().collection(REQUESTS).document(request_id).set({
        "id": request_id,
        "requirements_payload": requirements_payload,
        "request_status": "pending",
        "attempt_count": 0,
        "max_retries": max_retries,
        "next_retry_at": None,
        "created_on": _server_ts(),
        "modified_on": _server_ts(),
    })
    return request_id


def get_request(request_id: str) -> dict | None:
    snap = client().collection(REQUESTS).document(request_id).get()
    return snap.to_dict() if snap.exists else None


def update_request(request_id: str, **fields) -> None:
    """Patches request fields and bumps modified_on."""
    fields["modified_on"] = _server_ts()
    client().collection(REQUESTS).document(request_id).update(fields)


def set_request_status(request_id: str, status: str) -> None:
    update_request(request_id, request_status=status)


def list_requests() -> list[dict]:
    return [d.to_dict() for d in client().collection(REQUESTS).stream()]


# --------------------------------------------------------------------------- #
# 2. restaurants
# --------------------------------------------------------------------------- #

def upsert_restaurant(restaurant_id: str, name: str, lat=None, lon=None,
                      contact: str | None = None,
                      max_seating_capacity: int | None = None,
                      rating: float | None = None,
                      cuisine_types: list | None = None,
                      comments_from_restaurant: str | None = None) -> str:
    """Creates or updates a restaurant document."""
    doc = client().collection(RESTAURANTS).document(restaurant_id)
    data = {
        "id": restaurant_id,
        "name": name,
        "contact": contact,
        "max_seating_capacity": max_seating_capacity,
        "rating": rating,
        "cuisine_types": cuisine_types or [],
        "comments_from_restaurant": comments_from_restaurant,
        "modified_on": _server_ts(),
    }
    if lat is not None and lon is not None:
        data["location"] = _geopoint(lat, lon)
    if not doc.get().exists:
        data["created_on"] = _server_ts()
    doc.set(data, merge=True)
    return restaurant_id


def get_restaurant(restaurant_id: str) -> dict | None:
    snap = client().collection(RESTAURANTS).document(restaurant_id).get()
    return snap.to_dict() if snap.exists else None


def list_restaurants() -> list[dict]:
    return [d.to_dict() for d in client().collection(RESTAURANTS).stream()]


# --------------------------------------------------------------------------- #
# 3. conversation_summaries  (per request + restaurant)
# --------------------------------------------------------------------------- #

def _pair_id(request_id: str, restaurant_id: str) -> str:
    return f"{request_id}__{restaurant_id}"


def set_conversation_summary(request_id: str, restaurant_id: str,
                             summary: str) -> str:
    doc_id = _pair_id(request_id, restaurant_id)
    client().collection(CONVERSATION_SUMMARIES).document(doc_id).set({
        "request_id": request_id,
        "restaurant_id": restaurant_id,
        "summary": summary,
        "created_on": _server_ts(),
    })
    return doc_id


def get_conversation_summaries(request_id: str) -> list[dict]:
    q = client().collection(CONVERSATION_SUMMARIES).where("request_id", "==", request_id)
    return [d.to_dict() for d in q.stream()]


# --------------------------------------------------------------------------- #
# 4. final_summaries  (one per request; doc id = request_id)
# --------------------------------------------------------------------------- #

def set_final_summary(request_id: str, summary: str) -> None:
    doc = client().collection(FINAL_SUMMARIES).document(request_id)
    data = {"request_id": request_id, "summary": summary, "modified_on": _server_ts()}
    if not doc.get().exists:
        data["created_on"] = _server_ts()
    doc.set(data, merge=True)


def get_final_summary(request_id: str) -> dict | None:
    snap = client().collection(FINAL_SUMMARIES).document(request_id).get()
    return snap.to_dict() if snap.exists else None


# --------------------------------------------------------------------------- #
# 5. user_confirmations  (per request + restaurant)
# --------------------------------------------------------------------------- #

def set_user_confirmation(request_id: str, restaurant_id: str,
                          status: str = STATUS_IN_QUEUED) -> str:
    valid = {STATUS_COMPLETED, STATUS_IN_QUEUED, STATUS_NOT_COMPLETED}
    if status not in valid:
        raise ValueError(f"status must be one of {valid}, got {status!r}")
    doc_id = _pair_id(request_id, restaurant_id)
    doc = client().collection(USER_CONFIRMATIONS).document(doc_id)
    data = {
        "request_id": request_id,
        "restaurant_id": restaurant_id,
        "status": status,
        "modified_on": _server_ts(),
    }
    if not doc.get().exists:
        data["created_on"] = _server_ts()
    doc.set(data, merge=True)
    return doc_id


def get_user_confirmations(request_id: str) -> list[dict]:
    q = client().collection(USER_CONFIRMATIONS).where("request_id", "==", request_id)
    return [d.to_dict() for d in q.stream()]
