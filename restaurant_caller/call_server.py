"""Webhook server that runs the phone conversation for outbound Twilio calls.

Flow for each call:
  1. /voice/start   Twilio fetches this when the call is answered. We speak
                    the greeting and listen for a reply.
  2. /voice/respond Twilio posts each transcribed reply here. The call agent
                    decides what to say next, or ends the call with a verdict.
  3. /voice/status  Twilio posts here when the call ends (completed, busy,
                    no-answer, ...).

Run from the project root (the folder containing restaurant_caller/):
    uvicorn restaurant_caller.call_server:app --port 8000
and expose it publicly (e.g. `ngrok http 8000`), setting PUBLIC_BASE_URL to
the public URL.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from . import conversation, db

VOICE = os.environ.get("TWILIO_VOICE", "Polly.Raveena")
LANGUAGE = os.environ.get("TWILIO_LANGUAGE", "en-IN")
# Twilio waits at most 15s for a webhook response; leave headroom.
AGENT_TIMEOUT_SECONDS = 11
MAX_SILENT_TURNS = 2
FINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}

app = FastAPI()


async def _twilio_params(request: Request, call_id: str) -> dict:
    """Returns the posted form params after checking the request is from Twilio."""
    params = dict(await request.form())
    signature = request.headers.get("X-Twilio-Signature")
    if signature:
        url = os.environ["PUBLIC_BASE_URL"].rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
        if not validator.validate(url, params, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        return params

    # Some Twilio requests (seen on the call-answer webhook) arrive unsigned.
    # Accept those only if they name this exact call on our account: the
    # CallSid is known only to Twilio and us, and call_id is a random UUID.
    call = db.get_call(call_id)
    if (
        not call
        or not call["provider_call_id"]
        or params.get("CallSid") != call["provider_call_id"]
        or params.get("AccountSid") != os.environ["TWILIO_ACCOUNT_SID"]
    ):
        raise HTTPException(status_code=403, detail="Unverified request")
    return params


def _twiml(response: VoiceResponse) -> Response:
    return Response(content=str(response), media_type="application/xml")


def _say_and_listen(call_id: str, text: str, silent_turns: int = 0) -> Response:
    response = VoiceResponse()
    gather = response.gather(
        input="speech",
        action=f"/voice/respond?call_id={call_id}&silent={silent_turns}",
        method="POST",
        language=LANGUAGE,
        speech_timeout="auto",
        timeout=6,
        action_on_empty_result=True,
    )
    gather.say(text, voice=VOICE, language=LANGUAGE)
    return _twiml(response)


def _say_and_hang_up(text: str) -> Response:
    response = VoiceResponse()
    response.say(text, voice=VOICE, language=LANGUAGE)
    response.hangup()
    return _twiml(response)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/voice/start")
async def voice_start(request: Request, call_id: str):
    params = await _twilio_params(request, call_id)
    answered_by = params.get("AnsweredBy", "")
    if answered_by.startswith("machine") or answered_by == "fax":
        db.update_call(
            call_id,
            verdict="inconclusive",
            summary=f"The call reached an answering machine ({answered_by}); no conversation took place.",
        )
        response = VoiceResponse()
        response.hangup()
        return _twiml(response)

    greeting = await conversation.start(call_id)
    if not greeting:
        return _say_and_hang_up("Sorry, wrong number. Goodbye.")
    return _say_and_listen(call_id, greeting)


@app.post("/voice/respond")
async def voice_respond(request: Request, call_id: str, silent: int = 0):
    params = await _twilio_params(request, call_id)
    speech = params.get("SpeechResult", "").strip()

    if not await conversation.is_active(call_id):
        # The server restarted mid-call, so the conversation state is gone.
        db.update_call(
            call_id,
            verdict="inconclusive",
            summary="The call was cut short because the call server lost its state.",
        )
        return _say_and_hang_up(
            "Sorry, I'm having a technical problem. The customer will follow up. Goodbye."
        )

    if not speech:
        if silent + 1 >= MAX_SILENT_TURNS:
            db.update_call(call_id, verdict="inconclusive", summary="Nobody responded on the call.")
            return _say_and_hang_up("I'm not able to hear anyone, so I'll try again later. Goodbye.")
        return _say_and_listen(call_id, "Sorry, I didn't catch that. Are you still there?", silent + 1)

    reply, ended = await conversation.take_turn(call_id, speech, timeout=AGENT_TIMEOUT_SECONDS)
    if ended:
        return _say_and_hang_up(reply)
    return _say_and_listen(call_id, reply)


@app.post("/voice/status")
async def voice_status(request: Request, call_id: str):
    params = await _twilio_params(request, call_id)
    status = params.get("CallStatus", "")
    call = db.get_call(call_id)
    if not call or not status or call["status"] in FINAL_STATUSES:
        return Response(status_code=204)

    fields = {"status": status}
    if status in FINAL_STATUSES and not call["verdict"]:
        fields["verdict"] = "inconclusive"
        fields["summary"] = (
            "The call ended before a verdict was reached"
            + ("." if status == "completed" else f" (call status: {status}).")
        )
    db.update_call(call_id, **fields)
    if status in FINAL_STATUSES:
        await conversation.end(call_id)
    return Response(status_code=204)
