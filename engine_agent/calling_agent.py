"""Calling Agent.

The engine agent spawns one calling agent per vendor, in parallel. Each calling
agent places the call to its assigned vendor, carries out the conversation using
the requirement context it was given, and returns the transcript of the call to
the engine agent.

There is no real telephony here: the agent produces a realistic transcript of
the call. Swap :func:`run_call` for an integration with a real voice/telephony
provider (the transcription step) without changing the engine agent.
"""

import json
import uuid

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent_models import build_model

# A lighter effort than the engine: each call is a bounded, scripted conversation.
_MODEL, _CONFIG = build_model("low")

APP_NAME = "vendor_calling_agent"

INSTRUCTION = """
You are a calling agent placing a phone call to a vendor on behalf of a client
who is planning an event. You are given the vendor's profile and the client's
requirements, and you must hold the call and return ONLY the transcript.

Conduct a natural, concise phone conversation that:
- introduces the call and states the event type, date, guest count, and location,
- asks whether the vendor is available and can meet the client's must-haves,
- asks about pricing relative to the client's budget,
- probes the nice-to-haves and any special needs,
- confirms next steps.

Play BOTH sides of the call realistically: the vendor answers from its profile
(availability, what it offers, rough pricing for its price tier) and may not be
able to meet every requirement.
- Don't contradict the profile's listed features.
- For a seeded catalog venue, if asked about a feature it clearly does NOT list,
  the vendor says it can't offer that.
- For a web-discovered venue (source = "web"), the profile is partial — its full
  capabilities aren't all known. For details not in the profile, give a plausible
  answer based on the venue's description/cuisine, or say the vendor will confirm
  and follow up. Don't flatly deny things just because they're missing from a
  partial profile.

Output format — the transcript only, no preamble or commentary:
Agent: ...
Vendor: ...
Agent: ...
(continue until the call wraps up)
"""


def build_calling_agent() -> Agent:
    """Builds a calling-agent instance (one is used per vendor call)."""
    return Agent(
        name="calling_agent",
        description="Calls a single vendor and returns the call transcript.",
        model=_MODEL,
        generate_content_config=_CONFIG,
        instruction=INSTRUCTION,
    )


def _call_prompt(vendor: dict, requirements: dict) -> str:
    """Builds the single user message that kicks off the call."""
    return (
        "VENDOR PROFILE:\n"
        f"{json.dumps(vendor, indent=2)}\n\n"
        "CLIENT REQUIREMENTS:\n"
        f"{json.dumps(requirements, indent=2)}\n\n"
        "Place the call to this vendor now and return the transcript."
    )


def _greeting(vendor: dict, requirements: dict) -> str:
    """Short opening line before the questions."""
    req = requirements
    return (
        f"Hello, this is an event planning assistant calling "
        f"{('about ' + vendor['name'] + '. ') if vendor.get('name') else ''}"
        f"on behalf of a client planning a {req.get('event_type', 'event')} "
        f"for about {req.get('guest_count', 'several')} guests"
        f"{(' on ' + str(req.get('date'))) if req.get('date') else ''}. "
        f"I have a few quick questions — please answer each after the tone."
    )


def _questions(requirements: dict) -> list[str]:
    """One question per requirement — asked and answered one at a time."""
    req = requirements
    qs = ["Are you available for this event, and can you host this group size?"]
    for m in (req.get("must_haves") or []):
        qs.append(f"Can you provide {str(m).replace('_', ' ')}?")
    if req.get("budget"):
        qs.append(f"Our budget is {req['budget']}. Can you work within that?")
    nice = req.get("nice_to_haves") or []
    if nice:
        pretty = ", ".join(str(n).replace("_", " ") for n in nice)
        qs.append(f"Do you also offer {pretty}?")
    qs.append("Roughly what would this cost, and whom should we follow up with?")
    qs.append("Lastly, is there anything else you would like to add or any other "
              "details you would like to share about your venue?")
    return qs


async def run_call(vendor: dict, requirements: dict, call_index: int = 0) -> str:
    """Runs one vendor call and returns the transcript.

    If Twilio is configured, this places a REAL multi-turn call: it asks each
    requirement one at a time, waiting for the vendor's spoken answer before the
    next. Otherwise it falls back to a simulated transcript. Each simulated call
    gets its own in-memory runner/session, so calls run independently.

    ``call_index`` is this call's position in the parallel batch; it selects the
    test override number when CALL_OVERRIDE_NUMBER is a comma-separated list.
    """
    from . import telephony

    if telephony.is_configured():
        to = telephony.override_number_for(call_index) or vendor.get("phone")
        if to:
            greeting = _greeting(vendor, requirements)
            questions = _questions(requirements)
            res = await telephony.place_call(to, greeting, questions)
            if res.get("status") == "error":
                return f"[Real call to {to} could not be placed: {res.get('error')}]"
            return (
                f"[Real phone call to {to} via Twilio — status: {res.get('status')}, "
                f"{res.get('answered', 0)}/{len(questions)} questions answered]\n"
                f"{res.get('transcript', '')}"
            )
        # No number to dial -> fall through to simulation.

    runner = InMemoryRunner(agent=build_calling_agent(), app_name=APP_NAME)
    user_id = "engine"
    session_id = f"call-{vendor['id']}-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    message = types.Content(
        role="user", parts=[types.Part(text=_call_prompt(vendor, requirements))]
    )

    transcript = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            transcript = "".join(
                part.text for part in event.content.parts if part.text
            )
    return transcript.strip()
