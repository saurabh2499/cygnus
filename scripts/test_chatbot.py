"""Isolated test of the Chatbot Agent (cygnus).

Drives a scripted conversation through the chatbot and shows its replies, the
tools it calls, and the final DB effect (requirements saved + job enqueued).

Uses a throwaway database so the running cron/engine are NOT involved — this
tests the chatbot component on its own. Requires ANTHROPIC_API_KEY.

Usage:  python scripts/test_chatbot.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mockfirestore import MockFirestore  # noqa: E402  in-memory Firestore, no GCP needed
from engine_agent import firestore_db as _fs  # noqa: E402
_fs.set_client(MockFirestore())

import engine_agent.db as db  # noqa: E402  (also loads .env -> ANTHROPIC_API_KEY)
from cygnus.agent import root_agent  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

APP = "cygnus_test"

# A scripted client. The first turn gives every required field so the agent can
# go straight to a summary; the second turn approves.
TURNS = [
    ("Hi! I'm planning a company offsite dinner for about 60 people in downtown "
     "Chicago, indoors. The date is December 12, 2026, budget around $8000 USD. "
     "Must-haves are catering and AV. Vegan options would be a nice-to-have. "
     "No special needs."),
    "The dinner runs about 3 hours. That's everything — please show me the summary.",
    "That summary looks perfect — yes, please approve it.",
]


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — the chatbot needs it. Add it to .env.")
        return 1

    runner = InMemoryRunner(agent=root_agent, app_name=APP)
    uid, sid = "tester", "s1"
    await runner.session_service.create_session(app_name=APP, user_id=uid, session_id=sid)

    for turn in TURNS:
        print("\n" + "=" * 70)
        print(f"USER: {turn}")
        print("-" * 70)
        msg = types.Content(role="user", parts=[types.Part(text=turn)])
        async for event in runner.run_async(user_id=uid, session_id=sid, new_message=msg):
            for part in (event.content.parts if event.content else []):
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    args = {k: v for k, v in (fc.args or {}).items()}
                    print(f"   🔧 TOOL CALL: {fc.name}({args})")
                elif getattr(part, "function_response", None):
                    fr = part.function_response
                    resp = fr.response
                    status = resp.get("status") if isinstance(resp, dict) else resp
                    extra = ""
                    if isinstance(resp, dict) and resp.get("request_id"):
                        extra = f"  request_id={resp['request_id']}"
                    if isinstance(resp, dict) and resp.get("missing_required_fields"):
                        extra = f"  missing={resp['missing_required_fields']}"
                    print(f"   ✅ TOOL RESULT: {fr.name} -> {status}{extra}")
            if event.is_final_response() and event.content:
                text = "".join(p.text for p in event.content.parts if p.text)
                if text.strip():
                    print(f"\nASSISTANT: {text.strip()}")

    # Verify what landed in the DB.
    print("\n" + "=" * 70)
    print("DB VERIFICATION (chatbot component in isolation)")
    print("-" * 70)
    reqs = db.list_requests()
    if not reqs:
        print("❌ No request was saved — approval did not persist.")
        return 1
    r = reqs[0]
    print(f"✅ Request saved: {r['request_id']}")
    print(f"   event_type : {r['event_type']}")
    print(f"   guests     : {r['guest_count']}")
    print(f"   location   : {r['location']}")
    print(f"   job_status : {r['job_status']}   (enqueued for the cron)")

    stored = db.get_requirements(r["request_id"])
    missing = [f for f in ("event_type", "guest_count", "date", "location", "budget", "must_haves")
               if not stored.get(f)]
    ok = r["job_status"] == "pending" and not missing
    print(f"   must_haves : {stored.get('must_haves')}")
    print(f"   nice_to_haves: {stored.get('nice_to_haves')}")
    if missing:
        print(f"⚠️  missing required fields in saved record: {missing}")
    print("\nRESULT:", "chatbot PASS ✅" if ok else "chatbot FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
