"""Call override numbers, kept in Google Secret Manager (never in Firestore or env vars).

The secret CALL_OVERRIDE_NUMBERS holds a comma-separated list. Nothing in the API can
read or change it. Update it in Secret Manager (`gcloud secrets versions add`); the calling
code reads the latest version when it dials, so changes apply without a redeploy.

Needs the Secret Manager API and `roles/secretmanager.secretAccessor` for whatever places
the calls (Cloud Run and Agent Engine). Without access it quietly returns no numbers and
the caller falls back to the CALL_OVERRIDE_NUMBER env var.
"""

import os
import time

SECRET_ID = os.environ.get("CALL_OVERRIDE_SECRET", "CALL_OVERRIDE_NUMBERS")
_TTL = 30  # seconds to reuse a read, so parallel calls don't each hit the API

_client = None
_cache: tuple[float, list[str]] = (0.0, [])


def set_client(client) -> None:
    """Injects a Secret Manager client (a fake in tests)."""
    global _client, _cache
    _client, _cache = client, (0.0, [])


def _sm():
    global _client
    if _client is None:
        from google.cloud import secretmanager
        _client = secretmanager.SecretManagerServiceClient()
    return _client


def _project() -> str | None:
    # FIRESTORE_PROJECT is the project ID; on Agent Engine GOOGLE_CLOUD_PROJECT is the number.
    return os.environ.get("FIRESTORE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")


def get_call_overrides() -> list[str]:
    global _cache
    if time.time() - _cache[0] < _TTL:
        return list(_cache[1])
    nums: list[str] = []
    try:
        resp = _sm().access_secret_version(
            name=f"projects/{_project()}/secrets/{SECRET_ID}/versions/latest")
        nums = [n.strip() for n in resp.payload.data.decode().split(",") if n.strip()]
    except Exception:  # no secret / no access / no credentials -> caller falls back to env
        nums = []
    _cache = (time.time(), nums)
    return list(nums)
