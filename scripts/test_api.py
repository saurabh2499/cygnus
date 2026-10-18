"""Isolated test of the Backend API.

Exercises all three spec endpoints plus the dashboard-support endpoints using
FastAPI's TestClient against a throwaway database. No LLM, no cron, no network.

  POST /requests                      register + enqueue
  GET  /requests                      list all
  GET  /requests/{id}/summary         result view (pending -> completed)
  POST /requests/{id}/status          approve a vendor
  GET  /vendors                       catalog
  GET  /                              dashboard HTML

Usage:  python scripts/test_api.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mockfirestore import MockFirestore  # noqa: E402  in-memory Firestore, no GCP needed
from engine_agent import firestore_db as _fs  # noqa: E402
_fs.set_client(MockFirestore())

import engine_agent.db as db                 # noqa: E402
from backend.api import app                   # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

_failures = 0


def check(name, ok, detail=""):
    global _failures
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ->  {detail}" if detail else ""))
    if not ok:
        _failures += 1


REQS = {"event_type": "offsite dinner", "guest_count": 60, "location": "downtown",
        "budget": "$8000", "must_haves": ["catering", "av"], "nice_to_haves": ["vegan"]}


def main():
    with TestClient(app) as c:
        # 1. Register
        reg = c.post("/requests", json={"request_id": "api-1", "requirements": REQS})
        check("POST /requests registers and enqueues (201)",
              reg.status_code == 201 and reg.json().get("status") == "queued",
              f"{reg.status_code} {reg.json()}")

        # 2. Validation: empty requirements rejected
        bad = c.post("/requests", json={"request_id": "api-x", "requirements": {}})
        check("POST /requests rejects empty requirements (400)", bad.status_code == 400,
              str(bad.status_code))

        # 3. List
        lst = c.get("/requests").json()
        check("GET /requests lists the request", any(r["request_id"] == "api-1" for r in lst),
              f"{len(lst)} request(s)")

        # 4. Summary pending (includes requirements for the UI)
        pend = c.get("/requests/api-1/summary").json()
        check("GET /summary shows pending + echoes requirements",
              pend["result_status"] == "pending" and pend["requirements"].get("event_type"),
              f'status={pend["result_status"]}')

        # 5. Simulate an engine result, then read completed
        db.record_call("api-1", "v_olive", "called", transcript="...")
        db.set_vendor_summary("api-1", "v_olive", "Olive Table: meets all", rank=1)
        db.set_final_summary("api-1", "completed", "Olive Table is the best fit.",
                             [{"rank": 1, "vendor_id": "v_olive", "vendor_name": "The Olive Table",
                               "reason": "meets all"}])
        done = c.get("/requests/api-1/summary").json()
        check("GET /summary reflects completed result + ranking",
              done["result_status"] == "completed" and len(done["ranking"]) == 1,
              f'status={done["result_status"]}, ranking={len(done["ranking"])}')

        # 6. Approve a vendor
        appr = c.post("/requests/api-1/status", json={"vendor_id": "v_olive", "approved": True})
        after = c.get("/requests/api-1/summary").json()
        olive = next(v for v in after["vendors"] if v["vendor_id"] == "v_olive")
        check("POST /status approves a vendor", appr.status_code == 200 and olive["approved"] is True)

        # 7. 404s
        c1 = c.get("/requests/nope/summary").status_code
        c2 = c.post("/requests/api-1/status", json={"vendor_id": "ghost"}).status_code
        check("404 for unknown request and unknown vendor", c1 == 404 and c2 == 404, f"{c1}/{c2}")

        # 8. Catalog + dashboard
        vendors = c.get("/vendors").json()
        root = c.get("/")
        check("GET /vendors returns the catalog", len(vendors) == 5, f"{len(vendors)} vendors")
        check("GET / serves the dashboard HTML",
              root.status_code == 200 and "Vendor Management" in root.text,
              f"{root.status_code}, {len(root.text)} bytes")

    print("-" * 56)
    print("RESULT:", "backend API PASS ✅" if not _failures else f"{_failures} FAILED ❌")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
