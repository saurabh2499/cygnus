import asyncio
import os
import re
import urllib.request

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from . import db

# Any status other than these means the call has ended.
ACTIVE_STATUSES = {"initiating", "queued", "ringing", "in-progress"}
MAX_WAIT_SECONDS = 60
REQUIRED_SETTINGS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL")


def _phone_number(restaurant: dict) -> str:
    country_code = re.sub(r"\D", "", restaurant["contact_country_code"] or "")
    number = re.sub(r"\D", "", restaurant["contact_number"] or "")
    return f"+{country_code}{number}"


def get_call_details(request_id: str, restaurant_id: str) -> dict:
    """Looks up the approved event requirements and the restaurant to call.

    Args:
        request_id: The request ID of the approved event requirements.
        restaurant_id: The ID of the restaurant in the restaurants table.

    Returns:
        The requirements, restaurant details, and the number that would be dialed.
    """
    requirements = db.get_requirements(request_id)
    if requirements is None:
        return {"status": "error", "message": f"No approved requirements found for request ID {request_id}."}
    restaurant = db.get_restaurant(restaurant_id)
    if restaurant is None:
        return {"status": "error", "message": f"No restaurant found with ID {restaurant_id}."}
    if not restaurant.get("contact_number"):
        return {"status": "error", "message": f"{restaurant['name']} has no contact number."}
    return {
        "status": "success",
        "requirements": requirements,
        "restaurant": restaurant,
        "phone_number": _phone_number(restaurant),
    }


def place_call(request_id: str, restaurant_id: str) -> dict:
    """Places a real phone call to the restaurant to check the event requirements.

    Only call this after the user has confirmed they want the call placed.

    Args:
        request_id: The request ID of the approved event requirements.
        restaurant_id: The ID of the restaurant to call.

    Returns:
        The call_id to check progress with, or an error.
    """
    details = get_call_details(request_id, restaurant_id)
    if details["status"] != "success":
        return details

    missing = [name for name in REQUIRED_SETTINGS if not os.environ.get(name)]
    if missing:
        return {"status": "error", "message": f"Missing configuration: {', '.join(missing)}."}
    base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")

    # Fail early if the call server isn't reachable, rather than letting the
    # restaurant pick up to silence.
    try:
        health = urllib.request.Request(
            f"{base_url}/health", headers={"ngrok-skip-browser-warning": "1"}
        )
        urllib.request.urlopen(health, timeout=5)
    except Exception as e:
        return {"status": "error", "message": f"Call server at {base_url} is not reachable: {e}"}

    # Answering machine detection is a paid feature that trial accounts reject.
    optional_params = {}
    if os.environ.get("TWILIO_MACHINE_DETECTION", "false").lower() == "true":
        optional_params["machine_detection"] = "Enable"

    call_id = db.create_call(request_id, restaurant_id)
    try:
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        # The status callback fires once, when the call ends (completed, busy,
        # no-answer, ...), which is all the call server needs.
        call = client.calls.create(
            to=details["phone_number"],
            from_=os.environ["TWILIO_FROM_NUMBER"],
            url=f"{base_url}/voice/start?call_id={call_id}",
            status_callback=f"{base_url}/voice/status?call_id={call_id}",
            **optional_params,
        )
    except TwilioRestException as e:
        db.update_call(call_id, status="failed", verdict="inconclusive", summary=f"Twilio error: {e.msg}")
        return {"status": "error", "message": f"Twilio couldn't place the call: {e.msg}"}

    db.update_call(call_id, provider_call_id=call.sid, status=call.status)
    return {
        "status": "success",
        "call_id": call_id,
        "restaurant": details["restaurant"]["name"],
        "phone_number": details["phone_number"],
    }


async def check_call_status(call_id: str, wait_seconds: int = 0) -> dict:
    """Checks the status and outcome of a restaurant call.

    Args:
        call_id: The call_id returned by place_call.
        wait_seconds: How long to wait for the call to finish before returning,
            up to 60. Use 0 to return the current status immediately.

    Returns:
        The call status, the restaurant's answer for each requirement asked
        about so far, and once finished, the verdict, summary, unmet
        requirements, and full transcript.
    """
    wait_seconds = max(0, min(wait_seconds, MAX_WAIT_SECONDS))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        call = db.get_call(call_id)
        if call is None:
            return {"status": "error", "message": f"No call found with ID {call_id}."}
        finished = call["status"] not in ACTIVE_STATUSES and call["verdict"] is not None
        if finished or loop.time() >= deadline:
            break
        await asyncio.sleep(3)

    return {
        "status": "success",
        "call_status": call["status"],
        "finished": finished,
        "verdict": call["verdict"],
        "summary": call["summary"],
        "unmet_requirements": call["unmet_requirements"],
        "requirement_results": call["requirement_results"],
        "transcript": call["transcript"],
    }
