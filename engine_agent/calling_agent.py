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
from google.adk.models.anthropic_llm import AnthropicGenerateContentConfig, AnthropicLlm
from google.adk.runners import InMemoryRunner
from google.genai import types

# A lighter effort than the engine: each call is a bounded, scripted conversation.
_MODEL = AnthropicLlm(model="claude-opus-5-5")
_CONFIG = AnthropicGenerateContentConfig(effort="low")

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
able to meet every requirement. Do not invent features the vendor's profile does
not list; if asked about something it lacks, the vendor says so.

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


async def run_call(vendor: dict, requirements: dict) -> str:
    """Runs one vendor call to completion and returns the transcript text.

    Each call gets its own in-memory runner and session, so calls are fully
    independent and safe to run concurrently.
    """
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
