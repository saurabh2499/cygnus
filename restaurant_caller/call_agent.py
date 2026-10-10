"""The agent that talks to restaurant staff during a phone call.

Each thing the restaurant says (transcribed by Twilio) is sent to this agent
as a user message; its text reply is spoken back with text-to-speech.

The agent works through a checklist built from the approved requirements,
asking about each item and recording the restaurant's answer as it goes.
"""

from typing import Optional

from google.adk.agents import Agent
from google.adk.models.anthropic_llm import AnthropicGenerateContentConfig, AnthropicLlm
from google.adk.tools import ToolContext

from . import db

VERDICTS = ("matches", "partial_match", "does_not_match", "inconclusive")
ANSWER_STATUSES = ("met", "not_met", "partial", "unclear")
NOT_SPECIFIED = {"", "none", "not specified", "n/a"}


def build_greeting(restaurant_name: str) -> str:
    return (
        f"Hello, is this {restaurant_name}? This is an automated AI assistant "
        "calling on behalf of a customer who is planning an event. Do you have "
        "a couple of minutes to answer a few questions about hosting it?"
    )


def _specified(value) -> bool:
    if isinstance(value, list):
        return bool(value)
    return value is not None and str(value).strip().lower() not in NOT_SPECIFIED


def build_checklist(requirements: dict) -> list[dict]:
    """Turns the approved requirements into the items to ask the restaurant about."""
    items = []
    if _specified(requirements.get("date")):
        items.append({"id": "date", "requirement": f"Available on {requirements['date']}"})
    if _specified(requirements.get("guest_count")):
        items.append({"id": "guest_count", "requirement": f"Can host {requirements['guest_count']} guests"})
    if _specified(requirements.get("duration")):
        items.append({"id": "duration", "requirement": f"Event duration of {requirements['duration']}"})
    for i, item in enumerate(requirements.get("must_haves") or [], 1):
        items.append({"id": f"must_have_{i}", "requirement": f"Must-have: {item}"})
    for i, item in enumerate(requirements.get("special_needs") or [], 1):
        items.append({"id": f"special_need_{i}", "requirement": f"Special need: {item}"})
    if _specified(requirements.get("budget")):
        items.append({"id": "budget", "requirement": f"Fits the budget of {requirements['budget']}"})
    for i, item in enumerate(requirements.get("nice_to_haves") or [], 1):
        items.append({"id": f"nice_to_have_{i}", "requirement": f"Nice-to-have: {item}"})
    return items


def format_checklist(checklist: list[dict]) -> str:
    return "\n".join(f"- [{item['id']}] {item['requirement']}" for item in checklist)


def record_answer(tool_context: ToolContext, item_id: str, status: str, details: str) -> dict:
    """Records the restaurant's answer for one checklist item.

    Call this as soon as the restaurant has answered about an item, before
    moving on to the next one.

    Args:
        item_id: The checklist item's id, e.g. "date" or "must_have_2".
        status: One of "met", "not_met", "partial", "unclear".
        details: What the restaurant actually said, including any prices,
            conditions, or alternatives offered.

    Returns:
        The items still left to ask about.
    """
    checklist = tool_context.state.get("checklist", [])
    item = next((i for i in checklist if i["id"] == item_id), None)
    if item is None:
        return {"status": "error", "message": f"Unknown item_id. Valid ids: {[i['id'] for i in checklist]}"}
    if status not in ANSWER_STATUSES:
        return {"status": "error", "message": f"status must be one of {ANSWER_STATUSES}"}

    answers = dict(tool_context.state.get("answers", {}))
    answers[item_id] = {"requirement": item["requirement"], "status": status, "details": details}
    tool_context.state["answers"] = answers
    # Save as we go, so the answers survive even if the call drops.
    db.update_call(tool_context.state["call_id"], requirement_results=list(answers.values()))

    remaining = [i["id"] for i in checklist if i["id"] not in answers]
    return {"status": "success", "remaining_items": remaining}


def end_call(
    tool_context: ToolContext,
    verdict: str,
    summary: str,
    closing_message: str,
    unmet_requirements: Optional[list[str]] = None,
    ended_early_reason: Optional[str] = None,
) -> dict:
    """Ends the phone call and records whether the restaurant fits the requirements.

    Call this once every checklist item has an answer recorded. Only end
    earlier if the restaurant can't or won't continue, and say why in
    `ended_early_reason`.

    Args:
        verdict: One of "matches", "partial_match", "does_not_match", "inconclusive".
        summary: A few sentences on what you learned: what they can offer,
            prices or packages quoted, conditions, and anything to follow up on.
        closing_message: The polite goodbye to say before hanging up.
        unmet_requirements: Requirements the restaurant can't meet, if any.
        ended_early_reason: Why the call is ending before every item was asked
            about, e.g. "wrong number", "they were too busy to talk".

    Returns:
        Confirmation that the call outcome was recorded, or the items still to ask about.
    """
    if verdict not in VERDICTS:
        return {"status": "error", "message": f"verdict must be one of {VERDICTS}"}
    answers = tool_context.state.get("answers", {})
    remaining = [i["id"] for i in tool_context.state.get("checklist", []) if i["id"] not in answers]
    if remaining and not ended_early_reason:
        return {
            "status": "error",
            "message": "Some checklist items haven't been asked about yet. Ask about them "
            "first, or pass ended_early_reason if the restaurant can't continue.",
            "remaining_items": remaining,
        }

    if ended_early_reason:
        summary = f"{summary} (Call ended early: {ended_early_reason}.)"
    tool_context.state["call_outcome"] = {
        "verdict": verdict,
        "summary": summary,
        "closing_message": closing_message,
        "unmet_requirements": unmet_requirements or [],
    }
    # Hang up right after this tool; no further model turn is needed.
    tool_context.actions.skip_summarization = True
    return {"status": "success"}


INSTRUCTION = """
You are an AI assistant on a live phone call with staff at {restaurant_name},
calling on behalf of a customer to check whether the restaurant can host their
event. You are speaking, not writing.

The call opened with this greeting, already spoken by you:
"{greeting}"

The customer's approved event requirements:
{requirements_text}

Checklist: ask the restaurant about every item, in this order:
{checklist_text}

What the restaurant's listing says (may be outdated, so confirm on the call):
{restaurant_text}

How to run the call:
- Once they agree to talk, briefly say what the event is (type and guest
  count), then go through the checklist one item at a time. Ask one question
  per turn.
- As soon as they answer about an item, call `record_answer` for it, then ask
  about the next item. If an answer covers several items, record each one.
- Keep going through the whole checklist even if an item isn't met, so the
  customer gets the full picture. If something isn't possible, ask whether
  there's an alternative before moving on.
- For the budget, ask for their price per person or package price, and any
  deposit or minimum spend.
- Keep each reply to one or two short sentences. No lists, markdown, emojis,
  or abbreviations; say numbers the way a person would say them.
- If they ask, you are an AI assistant calling on behalf of a customer. Don't
  invent details about the customer beyond the requirements above, and don't
  commit to a booking or payment; say the customer will follow up.
- Speech transcription can be imperfect. If an answer is unclear, ask them to
  repeat it rather than guessing.
- If they're busy, not interested, or it's the wrong number, thank them and
  call `end_call` with `ended_early_reason`.
- When every item has an answer recorded, call `end_call` with your verdict:
  - matches: every must-have, the date, capacity, and budget are satisfied.
  - partial_match: most things work, but some requirement isn't met or needs
    a compromise.
  - does_not_match: a must-have, the date, or capacity can't be met.
  - inconclusive: you couldn't get enough information.
"""

call_agent = Agent(
    name="restaurant_call_agent",
    description="Talks to restaurant staff on a phone call to check event requirements.",
    # Low effort keeps replies fast so the restaurant isn't left waiting in silence.
    model=AnthropicLlm(model="claude-opus-5-5", max_tokens=2048),
    generate_content_config=AnthropicGenerateContentConfig(effort="low"),
    instruction=INSTRUCTION,
    tools=[record_answer, end_call],
)
