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

try:
    from agent_models import build_model
except ModuleNotFoundError:
    from .model_factory import build_model

# A lighter effort than the engine: each call is a bounded, scripted conversation.
_MODEL, _CONFIG = build_model("low")

APP_NAME = "vendor_calling_agent"

INSTRUCTION = """
You are a calling agent placing a phone call to a vendor (a restaurant/venue, or a
hotel when the requirements' vendor_type is "hotel") on behalf of a client.
For a hotel, introduce the stay (ALWAYS state both the check-in and the check-out
date yourself, even if the vendor doesn't ask; the number of nights too; number of rooms and
guests, location), ask about room availability, the nightly rate versus the budget,
amenities (breakfast, wifi, parking...), and the cancellation policy. You are given the vendor's profile and the client's
requirements, and you must hold the call and return ONLY the transcript.

Conduct a natural, concise phone conversation that:
- FIRST confirms you reached the right place ("Am I speaking with <vendor name>?"),
  and only continues once the vendor confirms,
- introduces the call and states the event type, date, guest count, and location,
- asks whether the vendor is available and can meet the client's must-haves,
- asks for the price (for a hotel: the per-night rate for one room). NEVER mention
  the client's budget up front. Work out the total yourself (restaurant: the quote;
  hotel: nightly rate x rooms x nights). Only if that total is OVER the budget, say it
  is over budget and ask politely for a discount; otherwise don't mention the budget,
- probes the nice-to-haves and any special needs,
- confirms next steps.

Sound like a real person, not a script: use short sentences, natural fillers
("okay, great", "got it"), and react to what the vendor just said. If the vendor
asks you to repeat, rephrase, or hold on, do that politely (repeat the question
in simpler words, or wait) and only move on once they have actually answered.

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


def _nights(req: dict) -> str:
    """' (3 nights)' when both dates parse, else ''."""
    from datetime import date
    try:
        n = (date.fromisoformat(str(req["check_out"])) - date.fromisoformat(str(req["date"]))).days
    except (KeyError, ValueError):
        return ""
    return f" ({n} night{'s' if n != 1 else ''})" if n > 0 else ""


def _say_date(value) -> str:
    """'2026-12-01' -> 'December 1, 2026' (reads better aloud); other text unchanged."""
    from datetime import date
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _hotel_when(req: dict) -> str:
    """Always states both dates so the vendor hears the check-out without asking."""
    if req.get("date") and req.get("check_out"):
        return (f" with check-in on {_say_date(req['date'])} and check-out on {_say_date(req['check_out'])}"
                f"{_nights(req)}")
    return f" on {req['date']}" if req.get("date") else ""


def _greeting(vendor: dict, requirements: dict) -> str:
    """Just a hello; the first question confirms we reached the right place."""
    return "Hello!"


IDENTITY_PREFIX = "Am I speaking with"


def _identity_question(vendor: dict) -> str:
    return f"{IDENTITY_PREFIX} {vendor.get('name') or 'the right place'}?"


def _intro(requirements: dict) -> str:
    """Who we are and why we're calling, asked once identity is confirmed."""
    req = requirements
    if req.get("vendor_type") == "hotel":
        rooms, g = req.get("rooms"), req.get("guest_count")
        return (f"Great, thank you. I'm helping a client book a stay{_hotel_when(req)}"
                f"{f' for {g} guests' if g else ''}{f', needing {rooms} rooms' if rooms else ''}. "
                f"Do you have a minute for a couple of quick questions?")
    g = req.get("guest_count")
    when = f" on {req.get('date')}" if req.get("date") else ""
    return (
        f"Great, thank you. I'm helping plan a {req.get('event_type', 'get-together')} for about "
        f"{g or 'a few'} guests{when}. Do you have a minute for a couple of quick questions?"
    )


def _questions(vendor: dict, requirements: dict) -> list[str]:
    """A natural set of questions, asked one at a time.

    Pricing is asked FIRST (based on the group size) before we reveal our budget,
    then we negotiate toward the budget.
    """
    req = requirements
    if req.get("vendor_type") == "hotel":
        return [_identity_question(vendor), _intro(req)] + _hotel_questions(req)
    g = req.get("guest_count")
    when = f" on {req.get('date')}" if req.get("date") else ""
    qs = [_identity_question(vendor), _intro(req),
          f"First, would you be available{when} for a group of about {g or 'this size'}?"]

    must = req.get("must_haves") or []
    if must:
        pretty = ", ".join(str(m).replace("_", " ") for m in must)
        qs.append(f"Great. We're hoping for {pretty} — is that something you can do?")

    # Ask the restaurant to quote first, based on guest count (don't reveal budget yet).
    qs.append(f"For a group of around {g or 'that size'}, roughly what would it cost? "
              f"Do you have a per-person rate or a package?")

    # The budget is never volunteered: the webhook asks for a discount only if the
    # vendor's quote turns out to exceed it (see backend/api.py, call plan).

    nice = req.get("nice_to_haves") or []
    if nice:
        pretty = ", ".join(str(n).replace("_", " ") for n in nice)
        qs.append(f"Would you also be able to offer {pretty}?")

    qs.append("And who should we follow up with to confirm a booking?")
    qs.append("Anything else you'd like to share about your place before we wrap up?")
    return qs


def _hotel_questions(req: dict) -> list[str]:
    """Hotel call script: availability, nightly rate, budget, amenities, policy."""
    rooms, g = req.get("rooms"), req.get("guest_count")
    need = f"{rooms} rooms" if rooms else "a room"
    qs = [f"First, do you have {need} available{_hotel_when(req)}"
          f"{f' for {g} guests' if g else ''}?"]
    must = req.get("must_haves") or []
    if must:
        pretty = ", ".join(str(m).replace("_", " ") for m in must)
        qs.append(f"Great. We need {pretty} — does that come with the rooms?")
    stay = (f" for check-in {_say_date(req['date'])} and check-out {_say_date(req['check_out'])}"
            if req.get("date") and req.get("check_out") else "")
    qs.append(f"What is the per night rate for one room{stay}, and does that include taxes?")
    nice = req.get("nice_to_haves") or []
    if nice:
        pretty = ", ".join(str(n).replace("_", " ") for n in nice)
        qs.append(f"Would you also be able to offer {pretty}?")
    qs.append("What is your cancellation policy?")
    qs.append("And who should we follow up with to confirm the booking?")
    return qs


def _budget_number(budget) -> float | None:
    """First number in a budget string like '$8,000 USD' or '1,00,000'."""
    import re
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(budget or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _nights_count(req: dict) -> int:
    from datetime import date
    try:
        return max(0, (date.fromisoformat(str(req["check_out"]))
                       - date.fromisoformat(str(req["date"]))).days)
    except (KeyError, ValueError):
        return 0


def _call_plan(questions: list[str], req: dict) -> dict:
    """What the webhook needs to check the vendor's quote against the budget."""
    hotel = req.get("vendor_type") == "hotel"
    marker = "per night rate" if hotel else "roughly what would it cost"
    idx = next((i for i, q in enumerate(questions) if marker in q), None)
    return {
        "price_idx": idx, "budget": _budget_number(req.get("budget")),
        "mode": "hotel" if hotel else "restaurant",
        "guests": req.get("guest_count") or 1, "rooms": req.get("rooms") or 1,
        "nights": _nights_count(req) or 1,
    }


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
            questions = _questions(vendor, requirements)
            res = await telephony.place_call(to, greeting, questions,
                                             plan=_call_plan(questions, requirements))
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
