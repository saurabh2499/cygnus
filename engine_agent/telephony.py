"""Real, multi-turn phone calls via Twilio Programmable Voice.

The live backend for the calling agent. It places an outbound call and holds a
back-and-forth conversation: it greets the vendor, then asks each requirement
one at a time, waiting for the vendor's spoken answer before moving to the next
(the requirements are "cleared" one by one). It returns the full transcript.

The dialog is driven by the backend TwiML webhooks (/twiml/call and /twiml/turn),
which read/advance the per-call conversation state stored in the DB by token.
Twilio must be able to reach those webhooks over PUBLIC_BASE_URL (e.g. ngrok).

Config (from .env):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, PUBLIC_BASE_URL  (required)
  CALL_OVERRIDE_NUMBER   if set, ALL calls dial this number (test mode)
  TWILIO_VOICE           TTS voice (default "alice")
  TWILIO_LANGUAGE        TTS/speech language (default "en-US")

Calls are serialized by a global lock so a single test number isn't dialed by
several parallel vendor calls at once.
"""

import asyncio
import os
import time
import uuid

from . import db


def is_configured() -> bool:
    """True when Twilio credentials AND a public base URL for TwiML are present.

    Trial accounts can't use inline TwiML, so Twilio must fetch it from a public
    URL (PUBLIC_BASE_URL, e.g. an ngrok tunnel to the backend).
    """
    return all(os.environ.get(k) for k in
               ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                "PUBLIC_BASE_URL"))


def override_numbers() -> list[str]:
    """Call override numbers for test routing: those added through the API, else
    the CALL_OVERRIDE_NUMBER env var (comma-separated).

    e.g. "+1...A,+1...A,+1...B" routes the 1st & 2nd parallel calls to A and the
    3rd to B (useful for watching the parallel fan-out land on two phones).
    """
    try:  # numbers in the Secret Manager secret win over the env var
        stored = db.get_call_overrides()
    except Exception:
        stored = []
    if stored:
        return stored
    raw = os.environ.get("CALL_OVERRIDE_NUMBER", "") or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


def override_number() -> str | None:
    """The first override destination, if any (single-call/back-compat)."""
    nums = override_numbers()
    return nums[0] if nums else None


def override_number_for(index: int) -> str | None:
    """The override destination for the Nth call in a parallel batch.

    Indexes into the list; calls past the end reuse the last number.
    """
    nums = override_numbers()
    if not nums:
        return None
    return nums[index] if index < len(nums) else nums[-1]


def _client():
    from twilio.rest import Client
    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def _transcript_from_state(st: dict) -> str:
    """Builds a readable transcript from the conversation state."""
    lines = [f"Agent: {st.get('greeting', '')}".strip()]
    for qa in st.get("answers", []):
        lines.append(f"Agent: {qa.get('q', '')}")
        lines.append(f"Vendor: {qa.get('a', '(no response)')}")
    lines.append("Agent: Thank you, that's everything. We'll follow up by email. Goodbye.")
    return "\n".join(l for l in lines if l)


def _place_sync(to: str, greeting: str, questions: list, wait: int,
                plan: dict | None = None) -> dict:
    client = _client()
    token = uuid.uuid4().hex
    # Seed the conversation plan the webhook will drive, keyed by call token.
    db.set_call_state(token, {
        "greeting": greeting, "questions": questions,
        "answers": [], "idx": 0, "status": "in_progress", "transcript": None,
        **(plan or {}),
    })
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    try:
        # NOTE: no `method=` — trial Twilio accounts reject it. Twilio defaults to POST.
        call = client.calls.create(
            to=to, from_=os.environ["TWILIO_FROM_NUMBER"],
            url=f"{base}/twiml/call?token={token}",
        )
    except Exception as exc:  # surface a readable error to the engine
        return {"ok": False, "status": "error", "error": str(exc)}

    sid = call.sid
    status = call.status
    deadline = time.time() + wait
    st = None
    while time.time() < deadline:
        status = client.calls(sid).fetch().status
        st = db.get_call_state(token)
        if st and st.get("status") == "done":
            break
        if status in ("failed", "busy", "no-answer", "canceled"):
            break
        time.sleep(3)

    st = db.get_call_state(token) or {}
    transcript = st.get("transcript") or _transcript_from_state(st)
    answered = len(st.get("answers", []))
    return {"ok": status == "completed", "status": status, "sid": sid,
            "transcript": transcript, "answered": answered}


def _place_announcement_sync(to: str, message: str) -> dict:
    client = _client()
    token = uuid.uuid4().hex
    db.set_call_state(token, {"greeting": message, "questions": [], "answers": [],
                              "idx": 0, "status": "announce", "transcript": None})
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    try:
        call = client.calls.create(
            to=to, from_=os.environ["TWILIO_FROM_NUMBER"],
            url=f"{base}/twiml/call?token={token}")
        return {"ok": True, "status": "queued", "sid": call.sid}
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc)}


async def place_announcement(to: str, message: str) -> dict:
    """Places a one-shot call that just speaks a message (no questions).

    Used for the post-approval booking confirmation call. Fire-and-forget: it
    creates the call and returns (Twilio handles the actual call)."""
    return await asyncio.to_thread(_place_announcement_sync, to, message)


async def place_call(to: str, greeting: str, questions: list, wait: int = 240,
                     plan: dict | None = None) -> dict:
    """Places one real multi-turn call: asks each question, waits for the vendor's
    spoken answer, then moves to the next, and returns the transcript.

    Runs the blocking Twilio work in a thread so calls in the same batch fan out
    concurrently (bounded by the engine's per-request semaphore)."""
    return await asyncio.to_thread(_place_sync, to, greeting, questions, wait, plan)
