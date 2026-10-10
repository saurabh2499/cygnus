"""Runs one engine agent to process a single request.

The cron job calls :func:`process_request` once per claimed job. Each call
spins up its own in-memory runner and session for the engine agent, so jobs are
independent and can run concurrently.
"""

import uuid

from google.adk.runners import InMemoryRunner
from google.genai import types

from engine_agent import db
from engine_agent.agent import root_agent

APP_NAME = "vendor_engine_agent"


async def process_request(request_id: str) -> dict:
    """Drives the engine agent end to end for one request.

    Returns ``{"ok": bool, "status": <result_status>, "error": <str|None>}``.
    Success is judged from the DB (the engine marks ``request_summaries.status``
    'completed'), not from the model's prose, so a run that didn't actually
    finish is treated as a failure and left for the cron job to retry.
    """
    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    user_id = "cron"
    session_id = f"job-{request_id}-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Process request {request_id}.")],
    )

    try:
        async for _event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=message
        ):
            pass  # drive the agent to completion; results land in the DB
    except Exception as exc:  # model/backend errors become a retryable failure
        return {"ok": False, "status": "error", "error": str(exc)}

    result = db.get_request_result(request_id)
    status = result["result_status"] if result else "unknown"
    if status == "completed":
        return {"ok": True, "status": status, "error": None}
    return {"ok": False, "status": status,
            "error": f"engine finished without completing (status={status})"}
