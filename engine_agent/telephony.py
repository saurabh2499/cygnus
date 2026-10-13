"""Real phone calls via Twilio Programmable Voice.

This is the live backend for the calling agent. It places an actual outbound
call, speaks the event inquiry (text-to-speech), records the vendor's spoken
reply, and returns a transcript (our script + Twilio's transcription of the
reply).

Config (from .env):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER   (required)
  CALL_OVERRIDE_NUMBER   if set, ALL calls dial this number (test mode)
  TWILIO_VOICE           TTS voice (default "alice")
  TWILIO_LANGUAGE        TTS/transcription language (default "en-US")
  TWILIO_MACHINE_DETECTION   e.g. "Enable" (optional)

Calls are serialized by a global lock so a single test number isn't dialed by
several parallel vendor calls at once.

Interactive multi-turn AI conversation (the agent reacting live to what the
vendor says) needs a public webhook server — that's the next step and would use
PUBLIC_BASE_URL. This module does a single spoken inquiry + recorded reply,
which is a real call that works without a public endpoint.
"""

import asyncio
import os
import time
import uuid
from urllib.parse import quote

from . import db

_call_lock = asyncio.Lock()


def is_configured() -> bool:
    """True when Twilio credentials AND a public base URL for TwiML are present.

    Trial accounts can't use inline TwiML, so Twilio must fetch it from a public
    URL (PUBLIC_BASE_URL, e.g. an ngrok tunnel to the backend).
    """
    return all(os.environ.get(k) for k in
               ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                "PUBLIC_BASE_URL"))


def override_number() -> str | None:
    """A single destination all calls are routed to (test mode), if set."""
    return os.environ.get("CALL_OVERRIDE_NUMBER")


def _client():
    from twilio.rest import Client
    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def _twiml_url(token: str, script: str) -> str:
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    return f"{base}/twiml/call?token={token}&script={quote(script)}"


def _place_sync(to: str, script: str, wait: int) -> dict:
    client = _client()
    token = uuid.uuid4().hex
    try:
        # NOTE: do not pass `method=` — trial Twilio accounts reject it with
        # "Invalid or disallowed parameters". Twilio defaults the webhook to POST.
        call = client.calls.create(
            to=to, from_=os.environ["TWILIO_FROM_NUMBER"],
            url=_twiml_url(token, script),
        )
    except Exception as exc:  # surface a readable error to the engine
        return {"ok": False, "status": "error", "error": str(exc)}

    sid = call.sid
    status = call.status
    # Wait for the call to finish AND for the spoken reply to be captured.
    deadline = time.time() + wait
    reply = None
    while time.time() < deadline:
        status = client.calls(sid).fetch().status
        reply = db.get_call_reply(token)
        if reply and status in ("completed", "failed", "busy", "no-answer", "canceled"):
            break
        if status in ("failed", "busy", "no-answer", "canceled"):
            break
        time.sleep(3)
    if reply is None:
        reply = db.get_call_reply(token)   # final check

    return {"ok": status == "completed", "status": status, "sid": sid,
            "reply_text": reply}


async def place_call(to: str, script: str, wait: int = 150) -> dict:
    """Places one real call (serialized), returning status + the spoken reply."""
    async with _call_lock:
        return await asyncio.to_thread(_place_sync, to, script, wait)
