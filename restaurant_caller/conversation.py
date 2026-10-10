"""Runs the call agent's side of a conversation, independent of how the call is carried.

Kept separate from the Twilio webhook server so the conversation logic doesn't
depend on how the call is carried.
"""

import asyncio
import logging
from typing import Optional

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from . import db
from .call_agent import build_checklist, build_greeting, call_agent, format_checklist

logger = logging.getLogger("restaurant_caller")

APP_NAME = "restaurant_caller"
USER_ID = "restaurant"

session_service = InMemorySessionService()
runner = Runner(agent=call_agent, app_name=APP_NAME, session_service=session_service)


def clean_reply(text: str) -> str:
    """Removes stray internal markers (e.g. "antml:...") the model occasionally emits."""
    lines = [line for line in text.splitlines() if not line.strip().startswith("antml:")]
    return " ".join(line.strip() for line in lines if line.strip())


def format_requirements(requirements: dict) -> str:
    lines = []
    for key, value in requirements.items():
        if isinstance(value, list):
            value = ", ".join(value) if value else "none"
        lines.append(f"- {key.replace('_', ' ')}: {value}")
    return "\n".join(lines)


def format_restaurant(restaurant: dict) -> str:
    fields = [
        "restaurant_type", "cuisine", "address", "city", "capacity",
        "private_dining_space", "non_veg_served", "avg_cost_per_person", "currency",
    ]
    return "\n".join(f"- {f.replace('_', ' ')}: {restaurant.get(f)}" for f in fields)


async def start(call_id: str) -> Optional[str]:
    """Sets up the agent for a newly answered call and returns the greeting to speak."""
    call = db.get_call(call_id)
    if not call:
        return None
    requirements = db.get_requirements(call["request_id"])
    restaurant = db.get_restaurant(call["restaurant_id"])
    greeting = build_greeting(restaurant["name"])
    checklist = build_checklist(requirements)
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=call_id,
        state={
            "call_id": call_id,
            "restaurant_name": restaurant["name"],
            "greeting": greeting,
            "requirements_text": format_requirements(requirements),
            "restaurant_text": format_restaurant(restaurant),
            "checklist": checklist,
            "checklist_text": format_checklist(checklist),
            "answers": {},
        },
    )
    db.update_call(call_id, status="in-progress")
    db.append_transcript(call_id, "assistant", greeting)
    return greeting


async def is_active(call_id: str) -> bool:
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=call_id
    )
    return session is not None


async def end(call_id: str) -> None:
    await session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=call_id)


async def _run_agent_turn(call_id: str, text: str) -> str:
    reply_parts = []
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=call_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        if event.content and event.author == call_agent.name:
            for part in event.content.parts or []:
                if part.text and not part.thought:
                    reply_parts.append(clean_reply(part.text))
    return " ".join(p for p in reply_parts if p)


async def take_turn(call_id: str, restaurant_said: str, timeout: float = 20) -> tuple[str, bool]:
    """Gives the agent what the restaurant said.

    Returns what the agent says next, and whether the call should end after it.
    """
    db.append_transcript(call_id, "restaurant", restaurant_said)
    try:
        reply = await asyncio.wait_for(_run_agent_turn(call_id, restaurant_said), timeout)
    except Exception:
        logger.exception("Call agent turn failed for call %s", call_id)
        reply = "Sorry, could you please say that again?"
        db.append_transcript(call_id, "assistant", reply)
        return reply, False

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=call_id
    )
    outcome = session.state.get("call_outcome") if session else None
    if outcome:
        goodbye = " ".join(p for p in (reply, outcome["closing_message"]) if p)
        db.append_transcript(call_id, "assistant", goodbye)
        db.update_call(
            call_id,
            verdict=outcome["verdict"],
            summary=outcome["summary"],
            unmet_requirements=outcome["unmet_requirements"],
        )
        await end(call_id)
        return goodbye, True

    reply = reply or "Sorry, could you please repeat that?"
    db.append_transcript(call_id, "assistant", reply)
    return reply, False
