import uuid
from typing import Optional

from google.adk.tools import ToolContext

REQUIRED_FIELDS = [
    "event_type",
    "guest_count",
    "date",
    "location",
    "budget",
    "must_haves",
]
OPTIONAL_FIELDS = [
    "nice_to_haves",
    "special_needs",
    "duration",
    "notes",
]
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS


def update_requirements(
    tool_context: ToolContext,
    event_type: Optional[str] = None,
    guest_count: Optional[int] = None,
    date: Optional[str] = None,
    location: Optional[str] = None,
    budget: Optional[str] = None,
    must_haves: Optional[list[str]] = None,
    nice_to_haves: Optional[list[str]] = None,
    special_needs: Optional[list[str]] = None,
    duration: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Saves or updates event requirements the user has confirmed.

    Only pass the fields the user has provided or changed. Lists replace the
    previous value, so always pass the full list.

    Args:
        event_type: Kind of event, e.g. wedding, birthday party, corporate offsite.
        guest_count: Approximate number of attendees.
        date: Date or rough timeframe, e.g. "mid-December 2026".
        location: Rough location, e.g. city or area, indoor/outdoor preference.
        budget: Budget or budget range, with currency.
        must_haves: Non-negotiable items, e.g. catering, DJ, stage, parking.
        nice_to_haves: Optional items the user would like if possible.
        special_needs: Accessibility, dietary, kids, pets, permits, etc.
        duration: How long the event runs, e.g. "4 hours", "2 days".
        notes: Any other context.

    Returns:
        The current requirements and which required fields are still missing.
    """
    provided = {
        "event_type": event_type,
        "guest_count": guest_count,
        "date": date,
        "location": location,
        "budget": budget,
        "must_haves": must_haves,
        "nice_to_haves": nice_to_haves,
        "special_needs": special_needs,
        "duration": duration,
        "notes": notes,
    }
    requirements = dict(tool_context.state.get("requirements", {}))
    requirements.update({k: v for k, v in provided.items() if v is not None})
    tool_context.state["requirements"] = requirements

    # Any change invalidates a previous approval.
    tool_context.state["approval_status"] = "pending"
    tool_context.state["request_id"] = None

    missing = [f for f in REQUIRED_FIELDS if not requirements.get(f)]
    return {
        "status": "success",
        "requirements": requirements,
        "missing_required_fields": missing,
        "ready_for_summary": not missing,
    }


def get_requirements_summary(tool_context: ToolContext) -> dict:
    """Builds the requirements summary to present to the user for approval.

    Returns:
        A formatted summary, or an error listing missing required fields.
    """
    requirements = tool_context.state.get("requirements", {})
    missing = [f for f in REQUIRED_FIELDS if not requirements.get(f)]
    if missing:
        return {"status": "incomplete", "missing_required_fields": missing}

    def fmt(value):
        if isinstance(value, list):
            return ", ".join(value) if value else "None"
        return str(value) if value else "Not specified"

    lines = [
        f"- {field.replace('_', ' ').title()}: {fmt(requirements.get(field))}"
        for field in ALL_FIELDS
    ]
    return {"status": "success", "summary": "\n".join(lines)}


def _save_requirements(requirements: dict) -> str:
    """Saves the approved requirements and enqueues a job for the engine agent.

    Delegates to the shared engine store (``engine_agent.db``), which persists the
    requirements and adds a 'pending' job the cron picks up. Imported lazily so the
    chatbot doesn't pull in the engine/LLM stack until an approval actually happens.
    """
    from engine_agent import db as engine_db

    request_id = str(uuid.uuid4())
    engine_db.register_request(request_id, requirements)
    return request_id


def record_approval(
    tool_context: ToolContext, approved: bool, feedback: Optional[str] = None
) -> dict:
    """Records whether the user approved the requirements summary.

    Args:
        approved: True if the user explicitly approved the summary.
        feedback: The user's requested changes, if they did not approve.

    Returns:
        The recorded approval status and, if approved, the saved request_id.
    """
    requirements = tool_context.state.get("requirements", {})
    missing = [f for f in REQUIRED_FIELDS if not requirements.get(f)]
    if approved and missing:
        return {
            "status": "error",
            "message": "Cannot approve: required fields are missing.",
            "missing_required_fields": missing,
        }

    # Already approved and saved with no changes since: don't save a duplicate.
    if approved and tool_context.state.get("request_id"):
        return {
            "status": "success",
            "approval_status": "approved",
            "request_id": tool_context.state["request_id"],
            "requirements": requirements,
        }

    request_id = None
    if approved:
        try:
            request_id = _save_requirements(requirements)
        except Exception as e:
            return {"status": "error", "message": f"Failed to save requirements: {e}"}

    tool_context.state["approval_status"] = "approved" if approved else "changes_requested"
    tool_context.state["request_id"] = request_id
    if feedback:
        tool_context.state["approval_feedback"] = feedback
    return {
        "status": "success",
        "approval_status": tool_context.state["approval_status"],
        "request_id": request_id,
        "requirements": requirements,
    }


def get_request_status(request_id: str) -> dict:
    """Looks up the status and results for a previously submitted request.

    Use this when the user asks about an existing request by its request ID. It
    reports where the request is in processing and, once finished, the final
    ranked summary and the per-vendor results.

    Args:
        request_id: The request ID the user was given when their requirements
            were approved.

    Returns:
        The processing status and, if available, the final summary and ranking.
    """
    from engine_agent import db as engine_db

    result = engine_db.get_request_result(request_id)
    if result is None:
        return {
            "status": "not_found",
            "message": f"No request found with ID {request_id}.",
        }
    return {"status": "success", **result}
