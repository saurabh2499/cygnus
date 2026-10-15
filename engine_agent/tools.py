"""Tools for the Engine Agent.

These implement the engine's responsibilities against the shared store:
  1. check the status of past calls for a request,
  2. match the request's requirements against the vendor catalog,
  3. fan out calling agents in parallel (one per vendor) and collect transcripts,
  4. log a per-vendor summary, and
  5. log the final cross-vendor summary with a ranking.

Summarisation and ranking are the LLM's job (it reads the transcripts returned
by :func:`call_vendors` and calls the save tools); these functions handle
matching, orchestration, and persistence.
"""

import asyncio

from . import db, discovery
from .calling_agent import run_call

# Max vendor calls to run concurrently, to bound load on the model backend.
_MAX_PARALLEL_CALLS = 5


def _as_list(value) -> list[str]:
    """Normalises a requirement field (list, comma string, or None) to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    return [part.strip().lower() for part in str(value).split(",") if part.strip()]


def get_past_calls(request_id: str) -> dict:
    """Returns the status of calls already made to vendors for this request.

    Use this first so a retried job doesn't re-call vendors that already have a
    completed call on record.

    Args:
        request_id: The request to look up.

    Returns:
        The existing call records (vendor, status, whether summarised).
    """
    db.init_db()
    calls = db.get_calls(request_id)
    return {
        "status": "success",
        "request_id": request_id,
        "call_count": len(calls),
        "calls": [
            {
                "vendor_id": c["vendor_id"],
                "status": c["status"],
                "has_transcript": bool(c["transcript"]),
                "has_summary": bool(c["summary"]),
            }
            for c in calls
        ],
    }


def discover_vendors(request_id: str, limit: int = db.DEFAULT_VENDOR_LIMIT) -> dict:
    """Finds candidate venues for the request's location.

    Location-aware with caching: if this location has been searched before, it
    reuses the venues cached in the DB (no web search). Only a *new* location
    triggers a live web search (nearby + trending restaurants/cafes). Caches new
    results so the next request for the same location is a pure DB lookup.

    Prefer this over ``match_vendors`` for real requests; if it reports the search
    backend is unavailable or finds nothing, fall back to ``match_vendors``.

    Args:
        request_id: The request whose location/requirements drive the lookup.
        limit: Max candidate venues to return.

    Returns:
        The candidate vendors (with ids) and a ``source`` of "cache" (known
        location, from DB) or "web" (new location, freshly searched); or a status
        explaining why discovery was skipped so you can fall back to the catalog.
    """
    db.init_db()
    requirements = db.get_requirements(request_id)
    if requirements is None:
        return {"status": "error",
                "message": f"No approved requirements found for request {request_id}."}
    location = requirements.get("location")
    if not location:
        return {"status": "error", "message": "Request has no location to search near."}
    norm = location.strip().lower()

    def _payload(vendors, source):
        return {
            "status": "success", "source": source, "request_id": request_id,
            "location": location, "candidate_count": len(vendors),
            "vendors": [
                {"vendor_id": v["id"], "name": v["name"], "area": v.get("area"),
                 "features": v["features"], "why": v.get("description"),
                 "source_url": v.get("source_url")}
                for v in vendors
            ],
        }

    # Known location -> reuse cached venues from the DB, no web search.
    cached = db.get_web_vendors_for_location(norm)
    if cached:
        return _payload(cached[:limit], source="cache")

    # New location -> live web search, then cache for next time.
    result = discovery.discover(location, requirements, limit=limit)
    if result.get("status") != "success":
        return result  # 'unavailable' / 'no_results' -> engine falls back

    for v in result["vendors"]:
        v["search_location"] = norm
        db.upsert_vendor(v)
    return _payload(result["vendors"], source="web")


def match_vendors(request_id: str, limit: int = db.DEFAULT_VENDOR_LIMIT) -> dict:
    """Finds the best-fit vendors for a request by scoring catalog features.

    Scoring rewards vendors whose features cover the client's must-haves and
    nice-to-haves, whose capacity fits the guest count, and whose location and
    price tier match; vendors that can't seat the guests are excluded.

    Args:
        request_id: The request whose requirements to match.
        limit: Maximum number of vendors to return (defaults to the configured limit).

    Returns:
        The ranked candidate vendors with their scores and match reasons.
    """
    db.init_db()
    requirements = db.get_requirements(request_id)
    if requirements is None:
        return {"status": "error",
                "message": f"No approved requirements found for request {request_id}."}

    must = set(_as_list(requirements.get("must_haves")))
    nice = set(_as_list(requirements.get("nice_to_haves")))
    guests = requirements.get("guest_count") or 0
    location = str(requirements.get("location", "")).lower()

    scored = []
    for v in db.list_vendors():
        feats = set(v["features"])
        # Hard filter: must be able to seat the party (when a count is known).
        if guests and v["capacity"] and v["capacity"] < guests:
            continue

        must_hits = must & feats
        nice_hits = nice & feats
        score = 3 * len(must_hits) + len(nice_hits)
        reasons = []
        if must_hits:
            reasons.append(f"covers must-haves: {', '.join(sorted(must_hits))}")
        if nice_hits:
            reasons.append(f"covers nice-to-haves: {', '.join(sorted(nice_hits))}")
        if location and v["area"] and v["area"].lower() in location:
            score += 2
            reasons.append(f"in requested area ({v['area']})")
        if guests and v["capacity"]:
            reasons.append(f"seats {v['capacity']} (need {guests})")

        missing = must - feats
        scored.append({
            "vendor_id": v["id"],
            "name": v["name"],
            "phone": v["phone"],
            "score": score,
            "unmet_must_haves": sorted(missing),
            "reasons": reasons,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[: max(1, limit)]
    return {
        "status": "success",
        "request_id": request_id,
        "candidate_count": len(top),
        "vendors": top,
    }


async def call_vendors(request_id: str, vendor_ids: list[str]) -> dict:
    """Spawns calling agents in PARALLEL, one per vendor, and collects transcripts.

    Pass the vendor ids chosen from ``match_vendors``. Vendors that already have a
    completed call for this request are skipped (safe to retry). Each transcript
    is stored before this returns; read the transcripts in the result and then
    call ``save_vendor_summary`` for each and ``save_final_summary`` once.

    Args:
        request_id: The request being processed.
        vendor_ids: The vendor ids to call.

    Returns:
        The transcript (or error) for each vendor that was called.
    """
    db.init_db()
    requirements = db.get_requirements(request_id)
    if requirements is None:
        return {"status": "error",
                "message": f"No approved requirements found for request {request_id}."}

    db.set_final_summary(request_id, status="processing")

    catalog = {v["id"]: v for v in db.list_vendors()}
    already_done = {
        c["vendor_id"] for c in db.get_calls(request_id)
        if c["status"] in ("called", "summarized") and c["transcript"]
    }

    to_call = []
    unknown = []
    for vid in vendor_ids:
        if vid in already_done:
            continue
        if vid not in catalog:
            unknown.append(vid)
            continue
        to_call.append(catalog[vid])

    semaphore = asyncio.Semaphore(_MAX_PARALLEL_CALLS)

    async def _one(vendor: dict, call_index: int):
        async with semaphore:
            try:
                transcript = await run_call(vendor, requirements, call_index=call_index)
                return vendor["id"], transcript, None
            except Exception as exc:  # surface call failures per-vendor
                return vendor["id"], None, str(exc)

    results = await asyncio.gather(*[_one(v, i) for i, v in enumerate(to_call)])

    # Persist sequentially (single writer) after the parallel calls complete.
    transcripts = []
    for vendor_id, transcript, error in results:
        if error:
            db.record_call(request_id, vendor_id, status="failed")
            transcripts.append({"vendor_id": vendor_id, "status": "failed",
                                "error": error})
        else:
            db.record_call(request_id, vendor_id, status="called",
                           transcript=transcript)
            transcripts.append({"vendor_id": vendor_id, "status": "called",
                                "vendor_name": catalog[vendor_id]["name"],
                                "transcript": transcript})

    return {
        "status": "success",
        "request_id": request_id,
        "called": len([t for t in transcripts if t["status"] == "called"]),
        "skipped_already_called": sorted(already_done & set(vendor_ids)),
        "unknown_vendor_ids": unknown,
        "transcripts": transcripts,
    }


def save_vendor_summary(request_id: str, vendor_id: str, summary: str,
                        rank: int | None = None) -> dict:
    """Logs the per-vendor summary (and optional rank) for a call to the DB.

    Args:
        request_id: The request being processed.
        vendor_id: The vendor the summary is for (must have a recorded call).
        summary: A concise summary of what the vendor offered on the call.
        rank: Optional 1-based rank of this vendor among the called vendors.

    Returns:
        Whether the summary was saved.
    """
    db.init_db()
    ok = db.set_vendor_summary(request_id, vendor_id, summary, rank)
    if not ok:
        return {"status": "error",
                "message": f"No call on record for vendor {vendor_id} on request {request_id}."}
    return {"status": "success", "request_id": request_id, "vendor_id": vendor_id}


def save_final_summary(request_id: str, final_summary: str,
                       ranking: list[dict]) -> dict:
    """Logs the final cross-vendor summary and ranking, marking the request complete.

    The chatbot agent reads this to return the result for the request.

    Args:
        request_id: The request being processed.
        final_summary: A summary comparing the vendors across their calls.
        ranking: Ordered best-first, each item e.g.
            {"rank": 1, "vendor_id": "v_olive", "vendor_name": "The Olive Table",
             "reason": "..."}.

    Returns:
        Confirmation that the final summary was saved.
    """
    db.init_db()
    db.set_final_summary(request_id, status="completed",
                         final_summary=final_summary, ranking=ranking)
    # Mirror ranks onto the per-vendor call records for easy lookup.
    for item in ranking:
        vid = item.get("vendor_id")
        rank = item.get("rank")
        if vid and rank is not None:
            existing = {c["vendor_id"]: c for c in db.get_calls(request_id)}
            if vid in existing and existing[vid]["summary"]:
                db.set_vendor_summary(request_id, vid, existing[vid]["summary"], rank)
    return {"status": "success", "request_id": request_id, "ranked": len(ranking)}
