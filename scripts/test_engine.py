"""Isolated test of the Engine Agent.

Seeds one approved request (as the chatbot would) in a throwaway database, then
drives ONLY the engine agent on it and shows each step:
  1. get_past_calls      (retry-safe check)
  2. match_vendors       (best-fit from the catalog)
  3. call_vendors        (spawns calling agents in PARALLEL, returns transcripts)
  4. save_vendor_summary (one per vendor)
  5. save_final_summary  (cross-vendor summary + ranking)

Then verifies the DB result. Makes real Claude calls, so needs ANTHROPIC_API_KEY.
Cron/chatbot are not involved — this isolates the engine component.

Usage:  python scripts/test_engine.py
"""

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["VENDOR_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="vm_engine_"), "engine.db"
)

import engine_agent.db as db                      # noqa: E402 (also loads .env)
from engine_agent.agent import root_agent         # noqa: E402
from google.adk.runners import InMemoryRunner      # noqa: E402
from google.genai import types                     # noqa: E402

APP = "engine_test"
RID = "engine-test-1"
REQS = {
    "event_type": "company offsite dinner", "guest_count": 60,
    "date": "December 12, 2026", "location": "downtown Chicago", "budget": "$8000 USD",
    "must_haves": ["catering", "av"], "nice_to_haves": ["vegan"],
    "special_needs": [], "duration": "3 hours",
}


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — the engine needs it. Add it to .env.")
        return 1

    db.init_db()
    db.register_request(RID, REQS)            # simulate the chatbot's approved request
    print(f"Seeded approved request {RID}: {REQS['event_type']}, "
          f"{REQS['guest_count']} guests, must-haves={REQS['must_haves']}\n")

    runner = InMemoryRunner(agent=root_agent, app_name=APP)
    uid, sid = "tester", "s1"
    await runner.session_service.create_session(app_name=APP, user_id=uid, session_id=sid)
    msg = types.Content(role="user", parts=[types.Part(text=f"Process request {RID}.")])

    print("Driving the engine agent...\n" + "-" * 70)
    t0 = time.time()
    saved_summaries = 0
    async for event in runner.run_async(user_id=uid, session_id=sid, new_message=msg):
        for part in (event.content.parts if event.content else []):
            if getattr(part, "function_call", None):
                fc = part.function_call
                if fc.name == "discover_vendors":
                    print(f"   🔧 discover_vendors(request_id=…)  ← live web search near the location")
                elif fc.name == "call_vendors":
                    vids = (fc.args or {}).get("vendor_ids", [])
                    print(f"   🔧 call_vendors(vendor_ids={vids})  ← spawns {len(vids)} calling agents IN PARALLEL")
                elif fc.name == "save_vendor_summary":
                    print(f"   🔧 save_vendor_summary(vendor_id={(fc.args or {}).get('vendor_id')})")
                elif fc.name == "save_final_summary":
                    n = len((fc.args or {}).get("ranking", []))
                    print(f"   🔧 save_final_summary(ranking of {n} vendors)")
                else:
                    print(f"   🔧 {fc.name}({dict(fc.args or {})})")
            elif getattr(part, "function_response", None):
                fr = part.function_response
                resp = fr.response if isinstance(fr.response, dict) else {}
                if fr.name == "discover_vendors":
                    if resp.get("status") == "success":
                        names = [v.get("name") for v in resp.get("vendors", [])]
                        print(f"   ✅ discover_vendors -> {len(names)} live venues: {names}")
                    else:
                        print(f"   ↩️  discover_vendors -> {resp.get('status')} (falling back to catalog)")
                elif fr.name == "match_vendors":
                    names = [v.get("name") for v in resp.get("vendors", [])]
                    print(f"   ✅ match_vendors -> {len(names)} candidates: {names}")
                elif fr.name == "call_vendors":
                    print(f"   ✅ call_vendors -> called={resp.get('called')} "
                          f"(transcripts returned)")
                elif fr.name == "save_vendor_summary":
                    saved_summaries += 1
                elif fr.name == "save_final_summary":
                    print(f"   ✅ save_final_summary -> ranked={resp.get('ranked')}")
    elapsed = time.time() - t0
    print("-" * 70)
    print(f"Engine finished in {elapsed:.1f}s · saved {saved_summaries} per-vendor summaries\n")

    # --- Verify the DB result ------------------------------------------------
    print("=" * 70 + "\nDB VERIFICATION\n" + "-" * 70)
    result = db.get_request_result(RID)
    status = result["result_status"]
    ranking = result["ranking"]
    vendors = result["vendors"]
    called = [v for v in vendors if v["status"] in ("called", "summarized")]
    summarized = [v for v in vendors if v["summary"]]

    print(f"result_status : {status}")
    print(f"final_summary : {'present' if result['final_summary'] else 'MISSING'} "
          f"({len(result['final_summary'] or '')} chars)")
    print(f"vendors called: {len(called)}  ·  with summaries: {len(summarized)}")
    print("\nRanking:")
    for r in ranking:
        print(f"   #{r.get('rank')} {r.get('vendor_name', r.get('vendor_id'))} — {r.get('reason','')}")

    ok = (status == "completed" and bool(result["final_summary"])
          and len(ranking) >= 1 and len(summarized) == len(called) and len(called) >= 1)
    print("\nChecks:")
    print(f"  [{'PASS' if status=='completed' else 'FAIL'}] request marked completed")
    print(f"  [{'PASS' if len(called)>=1 else 'FAIL'}] vendors were called (parallel fan-out)")
    print(f"  [{'PASS' if len(summarized)==len(called) and called else 'FAIL'}] every called vendor has a summary")
    print(f"  [{'PASS' if ranking else 'FAIL'}] a final ranking was produced")
    print(f"  [{'PASS' if result['final_summary'] else 'FAIL'}] a final summary was produced")
    print("\nRESULT:", "engine PASS ✅" if ok else "engine FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
