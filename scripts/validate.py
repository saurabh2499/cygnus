"""Offline validation for the vendor-management system.

Exercises every component except the live LLM calls, so it runs fast and needs
no ANTHROPIC_API_KEY:

  1. all packages import
  2. DB initialises and seeds the vendor catalog
  3. vendor matching ranks/filters correctly
  4. job queue: register -> claim -> complete, and retry -> fail at the cap
  5. cron sweep drives jobs (engine run mocked)
  6. backend API endpoints (via TestClient)
  7. chatbot -> queue wiring and status lookup

Runs against a throwaway database so it never touches your real data.

Usage:  python scripts/validate.py
Exit code 0 = everything passed, 1 = at least one check failed.
"""

import os
import sys

# Make the project root importable no matter where this script is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use an in-memory Firestore BEFORE importing anything that reads the DB.
from mockfirestore import MockFirestore  # noqa: E402  in-memory Firestore, no GCP needed
from engine_agent import firestore_db as _fs  # noqa: E402
_fs.set_client(MockFirestore())

import asyncio  # noqa: E402

_failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _failures
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  ->  {detail}"
    print(line)
    if not ok:
        _failures += 1


def main() -> int:
    # 1. Imports -----------------------------------------------------------
    try:
        import cygnus  # noqa: F401
        import engine_agent  # noqa: F401
        import scheduler.cron as cron
        import backend.api  # noqa: F401
        check("imports: all packages load", True)
    except Exception as exc:
        check("imports: all packages load", False, repr(exc))
        return 1  # nothing else can run

    from engine_agent import db, tools

    # 2. DB init + vendor seed --------------------------------------------
    db.init_db()
    vendors = db.list_vendors()
    check("db: initialises and seeds vendors", len(vendors) == 5,
          f"{len(vendors)} vendors seeded")

    # 3. Vendor matching ---------------------------------------------------
    reqs = {
        "event_type": "offsite dinner", "guest_count": 90, "location": "downtown",
        "budget": "$8000", "must_haves": ["catering", "private_room", "av"],
        "nice_to_haves": ["vegan"],
    }
    db.register_request("match-test", reqs)
    m = tools.match_vendors("match-test")
    ids = [v["vendor_id"] for v in m["vendors"]]
    # Too-small venues (Sakura/80, Corner/50) must be excluded for 90 guests;
    # The Olive Table (covers all must-haves + downtown + vegan) must rank #1.
    check("match: excludes under-capacity vendors",
          "v_sakura" not in ids and "v_corner" not in ids, f"top={ids}")
    check("match: ranks best-fit first", ids and ids[0] == "v_olive",
          f"#1={ids[0] if ids else None}")

    # 4. Job queue + retry cap --------------------------------------------
    db.register_request("job-good", reqs, max_retries=2)
    created = db.sync_jobs_from_requests()  # match-test request had a job too
    claimed = db.claim_due_jobs(10)
    reclaim = db.claim_due_jobs(10)
    check("queue: claims pending jobs atomically",
          len(claimed) >= 1 and len(reclaim) == 0,
          f"claimed={len(claimed)} reclaim={len(reclaim)}")

    db.complete_job("job-good")
    s1 = db.fail_or_retry_job("job-good-x", "boom")  # unknown -> 'unknown'
    # Drive a real job to the retry cap.
    db.register_request("job-bad", reqs, max_retries=2)
    db.claim_due_jobs(10)
    r1 = db.fail_or_retry_job("job-bad", "boom", backoff_seconds=0)
    db.claim_due_jobs(10)
    r2 = db.fail_or_retry_job("job-bad", "boom", backoff_seconds=0)
    check("queue: retries then fails at the cap (max_retries=2)",
          r1 == "retry_scheduled" and r2 == "failed", f"{r1} -> {r2}")

    # 5. Cron sweep (engine mocked) ---------------------------------------
    async def fake_process(request_id: str):
        db.set_final_summary(request_id, "completed", "ok", [{"rank": 1, "vendor_id": "v_olive"}])
        return {"ok": True, "status": "completed", "error": None}

    cron.process_request = fake_process
    cfg = cron.CronConfig()
    cfg.retry_backoff = 0
    db.register_request("sweep-1", reqs)
    processed = asyncio.run(cron.run_sweep(cfg))
    sweep_status = db.get_request_result("sweep-1")["job_status"]
    check("cron: sweep processes due jobs", processed >= 1 and sweep_status == "completed",
          f"processed={processed}, sweep-1={sweep_status}")

    # 6. Backend API -------------------------------------------------------
    from fastapi.testclient import TestClient
    from backend.api import app

    with TestClient(app) as client:
        reg = client.post("/requests", json={"request_id": "api-1", "requirements": reqs})
        pending = client.get("/requests/api-1/summary")
        missing = client.get("/requests/does-not-exist/summary")
        # Simulate an engine result, then approve a vendor.
        db.record_call("api-1", "v_olive", "called", transcript="...")
        db.set_vendor_summary("api-1", "v_olive", "meets all", rank=1)
        db.set_final_summary("api-1", "completed", "best fit",
                             [{"rank": 1, "vendor_id": "v_olive", "vendor_name": "The Olive Table"}])
        done = client.get("/requests/api-1/summary")
        approve = client.post("/requests/api-1/status", json={"vendor_id": "v_olive", "approved": True})
        bad_vendor = client.post("/requests/api-1/status", json={"vendor_id": "nope"})

    check("api: POST /requests registers (201)", reg.status_code == 201, str(reg.status_code))
    check("api: GET summary reflects pending then completed",
          pending.json()["result_status"] == "pending"
          and done.json()["result_status"] == "completed",
          f'{pending.json()["result_status"]} -> {done.json()["result_status"]}')
    check("api: approving a vendor succeeds", approve.json().get("approved") is True)
    check("api: 404s for unknown request and vendor",
          missing.status_code == 404 and bad_vendor.status_code == 404,
          f"{missing.status_code}/{bad_vendor.status_code}")

    # 7. Chatbot -> queue wiring ------------------------------------------
    from cygnus.tools import _save_requirements, get_request_status

    rid = _save_requirements(reqs)
    job = db.get_request_result(rid)
    status_known = get_request_status(rid)["status"]
    status_unknown = get_request_status("no-such-id")["status"]
    check("chatbot: approval enqueues a job",
          job is not None and job["job_status"] == "pending", f"job_status={job['job_status']}")
    check("chatbot: status lookup works",
          status_known == "success" and status_unknown == "not_found",
          f"known={status_known}, unknown={status_unknown}")

    # Summary --------------------------------------------------------------
    print("-" * 56)
    if _failures:
        print(f"RESULT: {_failures} check(s) FAILED")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
