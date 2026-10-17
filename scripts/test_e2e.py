"""End-to-end integration test across ALL components, using the live services.

Unlike the per-component tests, this uses the real shared database and the
already-running cron worker and backend API:

  1. Chatbot (cygnus)  -> gather + approve -> request_id, job enqueued   [real LLM]
  2. Live cron worker  -> claims the job, spawns the real engine agent
  3. Engine + calling agents -> match, parallel calls, rank -> DB         [real LLM]
  4. Live backend API  -> GET summary (poll until completed)
  5. Live backend API  -> POST approve the top-ranked vendor

Prerequisites (this is an integration test, so the real system must be up):
  - ANTHROPIC_API_KEY set (in .env)
  - backend running:  uvicorn backend.api:app --port 8000
  - cron running:     python -m scheduler.cron --interval 30

Usage:  python scripts/test_e2e.py
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# NOTE: do NOT override VENDOR_DB_PATH — we use the real shared DB the live
# cron worker is polling, so this is a true integration test.

import engine_agent.db as db                 # noqa: E402 (loads .env)
from cygnus.agent import root_agent           # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types                # noqa: E402

API = "http://127.0.0.1:8000"
TURNS = [
    ("Hi! I'm planning a company offsite dinner for about 60 people in downtown "
     "Chicago, indoors, on December 12, 2026. Budget around $8000 USD. Must-haves "
     "are catering and AV. Vegan options would be nice. No special needs. "
     "Duration about 3 hours. Please go straight to the summary."),
    "That looks perfect — yes, please approve it.",
]


def api_get(path):
    with urllib.request.urlopen(API + path, timeout=10) as r:
        return r.status, json.loads(r.read())


def api_post(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ->  {detail}" if detail else ""))
    return ok


async def run_chatbot():
    """Drives the chatbot to approval; returns the request_id it issued."""
    runner = InMemoryRunner(agent=root_agent, app_name="e2e_chat")
    uid, sid = "e2e", "s1"
    await runner.session_service.create_session(app_name="e2e_chat", user_id=uid, session_id=sid)
    request_id = None
    for turn in TURNS:
        msg = types.Content(role="user", parts=[types.Part(text=turn)])
        async for event in runner.run_async(user_id=uid, session_id=sid, new_message=msg):
            for part in (event.content.parts if event.content else []):
                fr = getattr(part, "function_response", None)
                if fr and fr.name == "record_approval" and isinstance(fr.response, dict):
                    request_id = fr.response.get("request_id") or request_id
    return request_id


def main():
    # Preflight: services up? (use a JSON endpoint — "/" returns HTML)
    try:
        if api_get("/vendors")[0] != 200:
            raise RuntimeError
    except Exception:
        print(f"Backend not reachable at {API}. Start it first:\n"
              f"  uvicorn backend.api:app --port 8000\n  python -m scheduler.cron --interval 30")
        return 1
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — needed for the chatbot and engine.")
        return 1

    ok = True
    print("STEP 1 — Chatbot: gather + approve  (real LLM)\n" + "-" * 60)
    rid = asyncio.run(run_chatbot())
    ok &= check("1. chatbot approved and issued a request_id", bool(rid), rid or "none")
    if not rid:
        return 1

    # Confirm it was enqueued (chatbot -> shared DB wiring)
    status, summ = api_get(f"/requests/{rid}/summary")
    ok &= check("2. request visible via API and queued", status == 200,
                f"job_status={summ.get('job_status')}")

    print("\nSTEP 2/3 — Live cron claims it and runs the real engine…\n" + "-" * 60)
    print("   (the running cron polls every 30s; then the engine takes ~40s)")
    deadline = time.time() + 180
    last = None
    while time.time() < deadline:
        _, s = api_get(f"/requests/{rid}/summary")
        state = (s["job_status"], s["result_status"])
        if state != last:
            print(f"   [{int(time.time()%10000)}] job={state[0]} result={state[1]}")
            last = state
        if s["result_status"] == "completed" or s["job_status"] == "failed":
            break
        time.sleep(5)

    _, final = api_get(f"/requests/{rid}/summary")
    ok &= check("3. cron + engine completed the request",
                final["result_status"] == "completed", final["result_status"])
    ok &= check("4. engine produced a ranking via the full chain",
                len(final.get("ranking", [])) >= 1, f"{len(final.get('ranking', []))} ranked")

    if final["result_status"] == "completed" and final["ranking"]:
        print("\n   Ranked result (read through the live API):")
        for r in final["ranking"]:
            print(f"     #{r.get('rank')} {r.get('vendor_name', r.get('vendor_id'))} — {r.get('reason','')}")

        print("\nSTEP 4 — Approve the top vendor via the live API\n" + "-" * 60)
        top = final["ranking"][0]["vendor_id"]
        st, resp = api_post(f"/requests/{rid}/status", {"vendor_id": top, "approved": True})
        _, after = api_get(f"/requests/{rid}/summary")
        approved = any(v["vendor_id"] == top and v["approved"] for v in after["vendors"])
        ok &= check(f"5. approved top vendor ({top}) via API", st == 200 and approved)

    print("\n" + "=" * 60)
    print("RESULT:", "end-to-end PASS ✅" if ok else "end-to-end FAIL ❌")
    print(f"View it in the dashboard: {API}/  (request {rid})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
